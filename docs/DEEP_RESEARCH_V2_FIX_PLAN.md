# Deep Research V2 修复方案（分阶段 TDD）

> 依据：`docs/DEEP_RESEARCH_V2_DIAGNOSIS.md`。目标是补齐 V2 方案 P0 的治本点，**不大重写**。
> 原则：每阶段 RED→GREEN→REFACTOR；保留 hive 既有资产（ledger / governance / 失败语义 / routing_reminder / SSE）。
> 日期：2026-05-28 · 分支：main

---

## 成功标准（验收锚点）

对同一条 Web3 query 重跑后：

0. **执行前人审** — deep research 跑之前必经 澄清意图 → 出计划 → 用户确认；未确认计划不得 fan-out。
1. **单一语言** — report.md 整篇为用户语言，英文实体名内联可保留，但不出现整段外语。
2. **是报告不是拼接** — 有连贯分析性散文，没有 per-worker 块边界/重复小标题。
2b. **分析质量达标** — 合成是"整合非罗列"（矛盾被消解、证据按质量加权、有缺口分析），跑过一道对抗校验（cherry-picking/最强反方/so-what）。
3. **更快** — 目标 ≤ 25 分钟（对标 SOTA ~20 分钟），无串行 per-source LLM 风暴、有 per-worker 超时。
4. **不双跑** — 一条 query 只触发一次实际 run。
5. **回归绿** — `backend/tests/deep_research/` 全绿。

---

## 不动的边界（surgical）

- 不重写 kernel / invoke_agent / governance。
- 不动 SSE 流式（`api/deep_research.py`）与 writer 落盘结构。
- controller 路径（dormant）本次不碰。
- 旧合成路径（`synthesize_report`/`draft_report`/`review_report`/`_stitch_sections`）**先留作兜底**，到 P5 确认 V2 稳定后再清理。

---

## 阶段总览

| 阶段 | 治哪个症状 | 改动面 | 风险 | 依赖 |
|---|---|---|---|---|
| **P-A 执行前人审门** | 输出跑偏（最高优先） | 新增 `deep_research_plan` 工具 + 已确认计划注入 + 硬门路由 | 中 | 无 |
| **P-Q 提示词质量对齐** | 质量第一性（最高杠杆） | 对标 academic-research-skills 重写 worker/合成提示词 + 源评级 + 对抗校验 | 中 | 织入 P-A/P0/P1 |
| **P0 语言锁定** | ① 中英混 | worker + synthesis 提示词 + 1 个软门 | 低 | 无 |
| **P1 合成只吃压缩 digest** | ③ 拼接 | reasoner 合成入参 + worker digest 契约 | 中 | 无 |
| **P2 砍 per-source 串行 LLM** | ④ 慢 | orchestrator worker 路径 | 中 | 验证 evaluator 依赖 |
| **P3 超时 + 防重复** | ④ 尾延迟 / ② 次因 | schemas 默认 + handler 去重 | 低 | 无 |
| **P4 引用契约** | ③ 引用错配 | fetch 时分配 src_id | 中高 | P1 |
| **P5 清理半成品兜底** | 技术债 | 删旧合成路径 | 低 | P1-P4 稳定 |

**首个可交付（walking skeleton）= P-A + P0 + P1**：先堵住"跑偏"（执行前确认），再干掉混语 + 拼接。P2+P3 解决速度。P4/P5 收尾。

---

## P-A — 执行前意图对齐 + 计划确认门（硬门，最高优先）

> 决策已定（用户拍板）：**硬门**（新增 `deep_research_plan` 工具，不可绕过）+ **每次都先澄清**。

**目标**：deep research 跑之前必须 澄清意图 → 出计划 → 用户确认，杜绝"白跑一小时还跑偏"。

**新流程（agent 在 chat 中编排）**
1. 用户提出研究 → agent 调 `deep_research_plan`（只规划，不执行）。
2. `deep_research_plan` 返回：
   - `clarifying_questions`：范围 / 地域 / 时间窗 / 深度 / 这份研究要支撑什么决策（**每次必返至少一轮**）。
   - `proposed_plan`：lanes（label/goal/queries）+ worker 主题 + 预计源数/深度/耗时。
   - 不执行、无 fan-out（成本 ≈ 2-3 次 LLM）。
3. agent 把澄清问题 + 计划呈现给用户；用户答复 + 确认/改。
4. agent 调 `deep_research_start(plan=<已确认计划>)`（或 run），orchestrator **使用该计划、跳过内部重算**。

**硬门实现**
- 新工具 `deep_research_plan`（governance=safe，不浏览、无副作用）。
- `ResearchRequest` 增 `approved_plan` / `worker_topics` + `plan_confirmed`（无状态：plan JSON 经 agent/用户回传，不落库）。
- `run_deep_research` / orchestrator：命中 `approved_plan` 即用之，跳过 `build_research_plan`+`refine_plan`+`decide_next`（`orchestrator.py:246-252`、`:472`）。
- `deep_research_start`/`deep_research_run`：若 `plan_confirmed` 缺失 → 返回 `{ok:false, status:"needs_plan", recommended_tool:"deep_research_plan"}`，强制走规划（仿 `deep_research_run` full→async 路由 `deep_research.py:70`）。
- `SKILL.md` 增硬规则：未经 `deep_research_plan` 确认不得 start/run；把"澄清 + 计划确认"列为标准流程开头。

**RED**
- `test_plan_tool_returns_plan_without_execution`：`deep_research_plan` 返回 plan + clarifying_questions，且不产生 report.md。
- `test_start_without_confirmed_plan_is_gated`：`deep_research_start` 无 `plan_confirmed` → needs_plan，不执行、不后台调度。
- `test_orchestrator_uses_approved_plan`：传入 approved_plan 时不再调 build/refine/decide_next。

**验收**：一次真跑里，顺序为 出计划 → 用户确认 → 才 fan-out；transcript/steps.jsonl 可见该顺序。

---

## P-Q — 提示词质量对齐（对标 `Imbad0202/academic-research-skills`，最高杠杆）

> 用户洞察：**提示词是质量第一性**，提示词垃圾 → 输出垃圾。对标该库（v3.10，13-agent 研究管线）的方法论。
>
> ⚠️ **定位（务必牢记）：Hive deep research 是通用研究能力——任意领域（学术 / 市场 / 技术 / 政策 / 法律 / Web3…）。Web3 研究员只是一个调用方，不是引擎定位。** 所有提示词必须 **domain-general、按研究问题/agent 职责自适应领域**，绝不写死成某一行业。
>
> **关键判断（务必先对齐）：借方法论 DNA，不照搬学术 ceremony。** 该库虽 domain-agnostic，但绑定了**学术发表 ceremony**（PRISMA / RoB 2 / GRADE / APA 7.0 / 掠夺性期刊检测 / IRB）——这些是"证据综述/投稿"专属流程，硬塞进通用引擎是错配，且 13-agent×6-phase 与"20 分钟"对冲。我们只抽**领域无关的质量内核**，写成通用提示词，保持 Hive 精简的两层 orchestrator-worker 运行时。

### 6 个可迁移内核 → 升级哪个 Hive 提示词

| # | 借来的内核（出处） | 现状（Hive） | 落到哪 |
|---|---|---|---|
| 1 | **合成=整合非罗列**：头号反模式 Sequential Summarization（"A 发现 X。B 发现 Y。"=正是 Hive）；流程 证据矩阵→收敛/分歧→矛盾消解→缺口分析→整合叙事（`synthesis_agent.md`） | `synthesize_from_digests` 只说"用作 substrate、引 id"→缝合 | **P1** 重写合成提示词 |
| 2 | **研究问题工程**：FINER 评分 + 范围边界(in/out/assumptions) + 2-3 子问题（`research_question_agent.md`）；Phase 1 后必经用户确认 + DA Checkpoint 1（**正好印证 P-A 硬门**） | P-A 只产 lanes/queries | **P-A** 升级为真正的"研究简报" |
| 3 | **证据分级**：每源 A-F 评级{证据层级/方法/时效/利益冲突}；合成"按证据质量加权"（`source_quality_hierarchy.md`） | 有 SourceType 枚举但无评级、合成不加权 | **P-Q1**(新) + 合成 |
| 4 | **魔鬼代言人**：cherry-picking / 确认偏误 / 替代解释 / 最强反方 / "so what" / 缺失了什么（`devils_advocate_agent.md`） | **完全没有** | **P-Q2**(新)：合成前一道轻量 DA |
| 5 | **反模式+质量标准入提示词**：每条必引；矛盾披露；缺口/局限透明；不抬高来源层级；灰区=FAIL | 机械门(数字/实体计数) | P0/P1 提示词 + 升级质量门 |
| 6 | **输出语言纪律**："跟随用户语言，术语保留英文"（`SKILL.md` Output Language） | 全链路无语言约束 | **P0** |

### 通用证据分级（domain-adaptive，替代学术 RCT 金字塔）
- **不绑定单一领域**。引擎按研究问题/agent 职责推断领域，再决定"什么算一手/权威"。参照该库 `source_quality_hierarchy.md` 的 **Field-Specific Adjustments**（医学=RCT/meta、技术=industry reports、政策=专家 panel、人文=primary sources…）。
- 通用四档（每档的"代表源"随领域变）：
  - **Tier 1 主证据/权威**：一手与权威——官方文档/披露、监管/政府文件、原始数据集、标准规范；学术域=同行评审/meta，加密域=链上数据/审计，技术域=一手规范/基准。
  - **Tier 2 强二手**：有信誉机构/分析方，方法可核验。
  - **Tier 3 二手 press**：主流 / 行业媒体。
  - **Tier 4 弱**：博客 / 社媒 / 匿名 / 未署名。
- worker 给每源标 **领域 + Tier + A-F 级**；合成"按证据质量加权，**Tier 4 不得单独支撑关键结论**"（呼应 anti-patterns「source tier inflation」+「cherry-picking」）。

### 速度护栏（不与 ≤25 分钟目标冲突）
- 不引入 13-agent / 6-phase / 多轮 revision committee。
- DA = **合成前一道**轻量 pass（非 3 个强制 checkpoint）。
- 源评级在 worker 那遍**就地产出**（不另起 per-source LLM，呼应 P2）。
- 一句话：**借提示词质量，不借流程铺张。**

### RED（示例）
- `test_synthesis_prompt_has_integration_antipatterns`：合成提示词含"integration not summarization" + 三反模式。
- `test_worker_emits_source_grade`：worker digest 每源带 Tier + A-F。
- `test_devils_advocate_pass_runs_before_finalize`：合成前跑 DA，产出 cherry-picking / 缺口 / 最强反方。
- `test_rq_brief_has_finer_scope_subquestions`：plan 工具产出含 FINER/范围/子问题的研究简报。

**验收**：合成提示词/worker 提示词体现上述内核；DA pass 有产出且写盘；研究简报含 FINER+范围+子问题。

---

## P0 — 语言锁定（治症状①）

**RED**
- `test_worker_prompt_pins_language`：`_build_worker_prompt`/`_build_worker_system_prompt` 含目标语言指令。
- `test_synthesis_prompt_pins_language`：`synthesize_from_digests` 提示词含 "write entirely in {lang}"。
- `test_language_gate_flags_foreign_paragraphs`：整段外语 ≥2 段 → 门 fail；中文报告内联英文实体名 → 门 pass（防误杀）。

**改动**
- `schemas.py`：`ResearchRequest` 增 `output_language: str = ""`；`from_arguments` 解析。
- 新增 `_resolve_language(request)`：显式优先；否则按 question 的 CJK 占比判定 → 返回人读标签（如 "Chinese (简体中文)" / "English"）。
- `worker.py`：两个 prompt 注入 "Write your entire output in {lang}. Translate every fact/quote/entity description into {lang}. Never mix languages."
- `reasoner.py:568`：`synthesize_from_digests` 提示词同样注入。
- `orchestrator.py:880`：`_evaluate_synthesis_quality` 增 **段落级**语言一致性检测（仅当 ≥2 个完整段落为非目标语言才 fail；避免内联实体名误杀）。

**验收**：单测绿 + 重跑报告整篇单语。

---

## P1 — 合成只吃压缩 digest，去冗余（治症状③，核心治本）

**RED**
- `test_synthesis_payload_excludes_raw_redundancy`：`synthesize_from_digests` 入参**不再同时**塞 `source_notes` 全文 + `lane_summaries` 全文 + `claims` 全文；只留压缩 worker digests + 极简 source 元数据（id/title/publisher/url/lane）。
- `test_worker_digest_has_format_contract`：worker digest 命中固定小节 + 长度上限。
- `test_synthesis_not_stitched`（fixture）：给 3 个不同语言/风格的 digest，合成输出无逐块边界、无重复小标题。

**改动**
- `worker.py:187`：digest 强格式契约 —— 固定小节（要点 / 关键数字与实体 / 矛盾与弱证据 / 缺口），明确长度上限（目标 5–8K），便于直接组装。
- `reasoner.py:504-567`：`synthesize_from_digests` 入参精简为「压缩 digests + 最小 source 元数据」；**移除**同时喂 notes/lane/claims 全文的冗余（引用所需的 src 列表仍透传）。
- 评估是否把单次合成升级为轻量 outline→merge（**不回到** 旧 `_stitch_sections`）；若单次强提示已达标则保持单次。

**验收**：fixture 测试证明是合成非缝合 + 回归绿。

---

## P2 — 砍掉 per-source 串行 LLM（治症状④）

**RED**
- `test_worker_path_no_serial_per_source_llm`：worker 路径下不再对每个 source 串行调 `extract_claims`+`summarize_source`。
- `test_attribution_gate_still_passes_without_per_source_notes`：去掉后归因门仍可过（claims 由 worker 产出或确定性回填）。

**改动**
- `orchestrator.py:287-318`：worker 路径移除 per-source 串行的 `_maybe_extract_claims`+`_maybe_summarize_source`（消化职责已归 worker）。
- 若 ledger 仍需 claims：让 worker 在自己那遍结构化产出 claims，或保留**确定性** `extract_claims_from_source`（无 LLM）。
- 先核实 `evaluator.evaluate` 的 `attribution` 门对 claims/notes 的依赖，必要时调整阈值或来源。

**验收**：重跑 steps.jsonl 无 per-source LLM 风暴；总时长显著下降。

---

## P3 — 超时 + 防重复（治症状④尾延迟 / ②次因）

**RED**
- `test_default_deadline_applied`：`deadline_seconds` 为空时套用合理默认（如 standard 600s / full 1200s）。
- `test_duplicate_run_deduped`：同 agent 同 question 短窗内第二次调用被去重/复用。

**改动**
- `schemas.py:73`：`from_arguments` 按 depth 给 `deadline_seconds` 默认（保留显式覆盖）。
- `orchestrator.py:535`：确认 per-worker `asyncio.wait_for` 在默认 deadline 下生效。
- `tools/handlers/deep_research.py`：按 (agent_id, question hash) 记在途任务，`deep_research_run`/`deep_research_start` 命中即返回既有 task_id，不重复执行。

**验收**：卡住 worker 会被掐；重复调用不双跑。

---

## P4 — 引用契约（治症状③引用错配）

**RED**
- `test_worker_sources_get_stable_ids_before_digest`：worker 抓取的源在进入 digest 前已可映射到稳定 src_id。
- `test_synthesis_no_invented_src`：合成不再臆造 src_id。

**改动**
- 抓取（`worker.py` on_tool_call / 父 ledger 接管点）即分配 src_id，digest 引用锚可被父 ledger 稳定回填。
- `synthesize_from_digests`：引用只做透传，不猜测映射。

**验收**：`_unknown_source_refs` 恒为空 + 引用整洁。

---

## P5 — 清理半成品兜底（技术债）

- 确认 V2 稳定后，删 `reasoner.py` 的 `draft_report`/`review_report`/`_stitch_sections`/`synthesize_report` 旧路与 `orchestrator.py:757-815` 的多路径分支，避免互相掩盖。
- 同步更新 SKILL.md（`SKILL.md:78` summarize 规则）避免 agent 再产第二段重复文字。

---

## 落地后的人工验证（必须真跑）

1. 用原 Web3 query（depth=full）重跑一次。
2. 核对：report.md 单语 ✓；分析性散文无逐块边界 ✓；`steps.jsonl` 无 per-source LLM 风暴 ✓；总时长 ✓；transcript 只有一次 deep_research_* 实际执行 ✓。
3. `backend` 全量回归 + `deep_research/` 定向测试绿。

---

## 落地状态（2026-05-28，已实现并测试）

首批 C1–C4 已全部落地到实际代码并 TDD 通过（`tests/deep_research/` + `tests/tools/` 267 passed，tool-surface/pack/capability/skill-seeder 46 passed，ruff 通过）。

| Chunk | 状态 | 关键改动 | 新测试 |
|---|---|---|---|
| C1 语言+整合合成 | ✅ | `language.py`(新,解析+段落级一致性)、`worker.py` digest 契约+语言、`reasoner.build_digest_synthesis_instruction`(整合非罗列三反模式)、`orchestrator` 语言门 | `test_language_lock.py` |
| C2 证据分级+提速 | ✅ | `grading.py`(新,Tier+A-F,domain-adaptive)、`ledger.add_source` 就地分级、合成按 tier 加权、worker 路径**移除 per-source 串行 LLM** | `test_source_grading.py` |
| C3 魔鬼代言人 | ✅ | `reasoner.devils_advocate_review`、orchestrator 合成前一道对抗 pass、`devils_advocate.jsonl` 落盘+SSE+合成消化 | `test_devils_advocate.py` |
| C4 执行前硬门+超时去重 | ✅ | `schemas`(plan_confirmed/worker_topics/deadline 默认)、handler `needs_plan` 网关(复用 run/start,零新工具)、orchestrator 用已确认 topics、in-process 去重、SKILL 硬规则×2 | `test_plan_gate.py` |

**实现决策记录**：P-A 没有新增独立 `deep_research_plan` 工具——改为在现有 `deep_research_run`/`deep_research_start` 内做"未确认即返回 needs_plan+计划+澄清问题、确认后才执行"的两段式网关，避免 governance/capability/pack/surface 五处快照测试的大面积接线，blast radius 更小。证据分级用确定性映射（不再起 per-source LLM），同时实现"分级"与"提速"。

### 第二批（P4 + P5 + 提示词打磨）— 已落地（全后端 2856 passed，ruff 绿）

| 项 | 状态 | 关键改动 | 测试 |
|---|---|---|---|
| **P4 引用契约** | ✅ | worker 抓取时 `new_id("src")` 分配稳定 id；`ledger.add_source(source_id=...)` 保留该 id（冲突才重铸）；id 贯穿 worker→ledger→报告 | `test_citation_contract.py` |
| **P5 清理拼接兜底** | ✅ | worker 路径仅 `synthesize_from_digests`，失败即 `DeepResearchSynthesisFailed`（无 stitch 回退）；删 `draft_report`/`review_report`/`_stitch_sections`/`_SECTION_GUIDANCE` + orchestrator two-stage 分支；线性路径保留并升级 `synthesize_report`；删 2 个过时 two-stage 测试 | — |
| **提示词打磨(SOTA)** | ✅ | 新 `prompt_craft.py`(WRITING_QUALITY 反 AI 腔 + REASONING_CALIBRATION Toulmin/epistemic/IBE)；注入 `synthesize_from_digests`/`synthesize_report`/worker/devils_advocate(+missing_warrants/overclaims)/extract_claims | `test_prompt_quality.py` |

对标 `academic-research-skills` 已迁移的领域无关内核：synthesis_agent（整合非罗列）、source_quality_hierarchy（分级）、devils_advocate（对抗+Toulmin/IBE）、argumentation_reasoning_framework（warrant/epistemic status）、writing_quality_check（反 AI 腔）。未迁移学术 ceremony（PRISMA/RoB/GRADE/APA/引用 anchor 标记）——领域错配且与速度目标冲突。

**仍待**：真实 Railway 重跑验收（单语 / 整合非拼接 / 时长 ≤25min / 先计划后执行）。代码层全部完成。

---

## 风险与权衡

- **P1 去冗余**可能短期让某些"事实密度"指标波动 → 用 fixture 守住质量门，必要时把 notes 以**极简摘要**而非全文回喂。
- **P2 砍 claims** 若被 evaluator 归因门强依赖，需同步调门，避免误判 failed。
- **语言门**最大风险是误杀（内联英文实体名）→ 用段落级检测而非 token 级。
