# Deep Research V2 生产事故根因诊断

> 触发：在 Railway 用「Web3 研究员」跑了一次 deep research，耗时 1 小时+，最终报告中英文混杂、明显是子 agent 输出的原样拼接、且像"两个不同批次"黏在一起，完全不像一份报告。
>
> 状态：V2 orchestrator-worker（commit `aac7149`）已落地，但**只叠加未治本**。本文逐层定位根因，作为修复方案（见末尾 P0 清单）的依据。
>
> 日期：2026-05-28 · 分支：main · 代码已就地核实（file:line 截至本次会话）。

---

## 0. 一句话结论

**`aac7149` 只落地了"worker 会自己 search+digest"这半截，漏了 V2 方案 P0 最关键的三件事：合成层没真正"只吃压缩 digest"、全链路没锁语言、旧的 per-source 串行 LLM 处理没删而是叠在 worker 之上。** 结果：真正写最终报告的 `synthesize_from_digests` 被喂进 3~6×12K 的 worker 成品散文 + 三套冗余笔记，单次 `max_tool_rounds=1` 一把梭 → 弱模型做**抽取式缝合**，保留每个 worker 的语言与块边界。用户看到的"中英混杂 + 两个批次"，本质就是被原样缝合的 worker 数字段。

---

## 1. 完整执行流程（谁参与 / 产出什么）

入口：Agent 在 chat 里加载 `deep-research` SKILL → 调 `deep_research_run`（同步，quick/standard）或 `deep_research_start`（异步后台，full/flagship）。

```
deep_research_run / deep_research_start  (tools/handlers/deep_research.py)
  run_deep_research                       (orchestrator.py:424)
   └─ DeepResearchOrchestrator.run        (orchestrator.py:45)
       ├─ controller_mode?  → 否（默认 false）
       └─ _should_use_worker_path?         (orchestrator.py:458)  → 是 → 走 V2
          _run_worker_path                 (orchestrator.py:241)
            1. build_research_plan         planner.py            确定性，无 LLM
            2. reasoner.refine_plan        reasoner.py:39        LLM #1 (planner)
            3. reasoner.decide_next        reasoner.py:284       LLM #2 (planner) 选 3-6 worker 主题
            4. _run_worker_fanout          orchestrator.py:519   N worker，并发 min(concurrency=4,3)=3
                 每个 worker = 完整 invoke_agent(web 工具, 8-10 轮)   worker.py:88   ← 真·子 agent
            5. 对每个 worker 抓到的 source【串行】:
                 reasoner.extract_claims   reasoner.py:100       LLM × 源数
                 reasoner.summarize_source reasoner.py:127       LLM × 源数   ← 串行大瓶颈
            6. evaluator.evaluate + _aggregate_lane_summaries    确定性
            7. _synthesize_report          orchestrator.py:702
                 → reasoner.synthesize_from_digests  reasoner.py:504  LLM (writer)，最多 2 次  ← 最终报告
            8. _apply_footnotes / _evaluate_synthesis_quality / writer.finalize → report.md
```

一次任务的 LLM/agent 调用量 ≈ `2(计划) + N 个 worker × 多轮 + 2 × 源数(串行) + 1~2(合成)`。

落盘产物（`writer.py`）：`request.json` `plan.json` `steps.jsonl` `sources.jsonl` `claims.jsonl` `source_notes.jsonl` `lane_summaries.jsonl` `worker_reports.jsonl` `evaluation.jsonl` `report.md` `final.json`。

---

## 2. 四个症状的根因

### 症状① 报告一部分中文一部分英文

整条 V2 路径**没有任何一处锁定输出语言**：

- **Worker 提示词无语言约束** — `worker.py:187` `_build_worker_prompt`、`worker.py:204` `_build_worker_system_prompt`，一字未提语言。Worker 读的 Web3 源多为英文 → 数字段就是英文；偶尔命中中文源 → 那段中文。
- **真正用的合成函数无语言指令** — `synthesize_from_digests` 提示词 `reasoner.py:568-584` 没提语言。对比**没被走的** `synthesize_report`（`reasoner.py:484` "in the user's language"）与 `draft_report`（`reasoner.py:374` 同款）—— V2 这条路恰好漏了。
- **合成是拼接**（见③），把各 worker 的原语言**原样带出**。
- **质量门没有语言一致性检查** — `_evaluate_synthesis_quality`（`orchestrator.py:880`）查字数/数字/实体/未知引用，唯独不查语言。

→ 没有任何地方把语言收敛成一种，必然中英混。用户原话"有的子 agent 中文有的英文"是直接证据。

### 症状② 一次任务输出"两个不同批次"

`writer.py:63-100`：report.md 只写**一份**合成结果，worker 内容单独落 `worker_reports.jsonl`。所以"批次感"不来自 writer，有两个来源：

- **主因（最可能）**：`synthesize_from_digests` 把每个 worker 的 `intermediate_report[:12000]` 整段喂入（`reasoner.py:528-549`），单次合成弱模型做**抽取式缝合**，保留 per-worker 块边界与语言。3~6 个 worker 块拼起来 = 视觉上"多个批次报告黏在一起"。
- **次因（需运行记录确认）**：agent 层**可能跑了两次**。SKILL 路由 standard→`deep_research_run`、full→`deep_research_start`（`SKILL.md:42-43`），**没有任何去重**；若先同步跑 standard 觉得不行、又异步跑 full，就是两份报告 + 正好"1 小时+"。另 `SKILL.md:78` 要求 agent 额外自然语言 summarize 一遍 report.md，会再产出一段（通常中文）→ 也像"两批"。

> 确认次因的方法：看那次任务 `steps.jsonl` 里 `worker` step 数量，以及 chat transcript 里 `deep_research_*` 工具被调了几次。

### 症状③ 全是拼接、完全不像报告

V2 **没治本**的核心：

- **合成器吃的是"成品散文"而非"事实"**：`synthesize_from_digests` 一次收到 3~6×12K worker 报告 + `source_notes` + `lane_summaries` + `claims` + source 元数据（`reasoner.py:521-567`）——同一批证据的 **4 套冗余表示**，最多 70K+ 字符。把一堆写好的报告塞给 LLM 说"写最终报告"，它就缝合。
- **单次一把梭**：合成走 `reasoner._invoke → invoke_agent(max_tool_rounds=1)`（`reasoner.py:623`），无 outline→draft→revise、无思考预算。
- **V2 叠加而非替换**：方案 P0 要求"废弃 stitch/draft/review、合成只吃 digests"。但实际 commit：① 仍保留 per-source 的 `summarize_source`+`extract_claims`（`orchestrator.py:312-318`）；② 仍把 source_notes/lane_summaries/claims 与 worker digests 一起喂；③ `draft_report`/`review_report`/`_stitch_sections` 全留着当兜底（`orchestrator.py:757-815`、`reasoner.py:772`）。**digest 层没有真正压缩与隔离**（onyx 精髓：合成只看 5-10K 消化品，永不见原文/原始块）。
- **引用错配**：worker 没有 src_id，只能在散文里写 URL（`worker.py:200`）；最终却要求只用 src_id（`reasoner.py:573`）。合成器被迫做 URL→src_id 映射，做不好就更像拼贴。

### 症状④ 耗时 1 小时+（SOTA 仅 ~20 分钟）

慢点叠加：

1. **per-source 两次 LLM 串行** — 每源跑 `extract_claims`+`summarize_source` 共 2 次 `invoke_agent`，在 for 循环里**顺序执行**（`orchestrator.py:287-318`）。8 源 = 16 次串行重型调用。
2. **每次 reasoner 调用都是重型 `invoke_agent`** — 要装配完整 prompt（soul+记忆检索+工具目录）+治理+hooks，而非瘦 LLM 调用。整轮 20~50 次。
3. **worker 是完整 agent 多轮循环** — 每个 8-10 轮（`worker.py:215`），每轮可能 search+fetch（网络 10-30s）。
4. **`deadline_seconds` 默认 None**（`schemas.py:73`）→ `_run_worker_fanout` 的 `asyncio.wait_for` **没有 per-worker 超时**（`orchestrator.py:535-537`），卡住的 worker 不会被掐。SSE 那个 600s 只掐**流**不掐后台任务（`api/deep_research.py:42`）。
5. **合成 70K+ 字符 × 最多 2 次**。
6. 若 agent 真跑两次（②次因）→ 全部翻倍。

对比 Gemini/OpenAI/Claude DR：紧并行 + 每文档轻量抽取（常在 worker 同一遍完成，不另起 LLM）+ 单个强合成器只看紧凑 digest。本系统把每个源**串行重读两遍**、又把 digest 冗余四份，自然又慢又糊。

---

## 3. 各环节标准/提示词清单

| 环节 | 文件:行 | 提示词/标准 | 问题 |
|---|---|---|---|
| 总计划 | `planner.py` + `reasoner.refine_plan` `:39` | lanes+queries，JSON | 确定性兜底尚可 |
| 选 worker 主题 | `reasoner.decide_next` `:284` | "3-6 个独立主题" | OK |
| Worker 用户提示 | `worker.py:187` | "5-10 bullets，带数字/实体/矛盾" | **无语言、无长度上限、无格式契约** |
| Worker 系统提示 | `worker.py:204` | "只读 web，产 dense findings" | **无语言** |
| 每源抽 claims | `reasoner.extract_claims` `:100` | JSON，源绑定 | 串行、重复读源 |
| 每源结构化笔记 | `reasoner.summarize_source` `:127` | entities/numbers/dates… | 串行、与 worker 重复消化 |
| **最终报告** | `synthesize_from_digests` `:568` | 必含 Executive/Findings/Source 等节，引用 src_id | **无语言、吃 70K 冗余、单次一把梭** |
| 质量门 | `_evaluate_synthesis_quality` `:880` | 字数/数字数/实体数/未知引用 | **无语言一致性门** |
| 搜索依据 | `worker.py:19-24` | search 仅发现，fetch 才算证据 | OK |
| 证据存储 | `ledger.py` + `writer.py` | sources/claims/source_notes/lane_summaries/worker_reports.jsonl | 落盘完整 |
| 默认参数 | `schemas.py:69-73` | depth=standard, max_rounds=4, max_sources=8, concurrency=4, deadline=None | **无超时兜底** |

---

## 4. 修复方向（P0 治本清单，待转方案）

> 原则：不大重写，补齐 V2 方案 P0 的治本点。每条 TDD（先写 RED）。

1. **锁定单一输出语言（治症状①）**
   - 在 `_build_worker_prompt`/`_build_worker_system_prompt` 注入目标语言（从 `request` 推导用户语言，或新增 `request.output_language`）。
   - 在 `synthesize_from_digests` 提示词补 "write the entire report in {language}; translate all evidence into it, never mix languages"。
   - `_evaluate_synthesis_quality` 增加**语言一致性门**（检测混语比例超阈值即 fail）。

2. **合成只吃压缩 digest，去冗余（治症状③ + 部分④）**
   - 合成入参**只保留** worker digests（且压缩到 onyx 档 5-10K/worker）+ 极简 source 元数据；**移除**同时喂 source_notes/lane_summaries/claims 全文的冗余。
   - worker digest 增加**强格式契约**（固定小节 + 长度上限），让 digest 本身可直接组装。
   - 评估是否保留单次合成，或改 outline→section→merge 的轻量两段（但避免回到旧 stitch）。

3. **砍掉 per-source 串行 LLM（治症状④）**
   - worker 路径下**移除/合并** `summarize_source`+`extract_claims` 的 per-source 串行调用（`orchestrator.py:312-318`）——消化职责已归 worker；如需 claims，让 worker 在自己那遍里结构化产出。

4. **加超时与防重复（治症状④ + ②次因）**
   - 给 `request.deadline_seconds` 设合理默认 + per-worker 超时兜底。
   - agent 侧（SKILL/governance）对同一 question 去重，防止 run+start 双跑。

5. **修引用契约（治症状③引用错配）**
   - 让 worker 产出可被父 ledger 稳定映射的引用锚（如 URL→src_id 在抓取时即分配），合成器只做引用透传不做猜测。

6. **清理半成品兜底**
   - 评估废弃 `draft_report`/`review_report`/`_stitch_sections`（`reasoner.py:330-444,772`）与 `synthesize_report` 旧路（确认 V2 稳定后），避免多路径互相掩盖。

---

## 5. 关联文档

- `docs/DEEP_RESEARCH_REVAMP_PLAN.md` — v1.1 线性管线优化（已落地）
- `docs/DEEP_RESEARCH_SOTA_V2.md` — v2.0 orchestrator-worker 方案（P0-P3）
- 本文 = v2.0 落地后的**生产事故根因诊断**，证明 P0 未真正完成
