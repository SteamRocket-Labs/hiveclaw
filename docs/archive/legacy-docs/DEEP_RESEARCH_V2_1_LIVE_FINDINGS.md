# Deep Research v2.1 — 线上实跑根因发现 + 下一批修复计划

> **状态**：v2 批次（commit `e56050c`：计划门/语言/整合合成/分级/魔鬼代言人/提速/引用/清理/SOTA 提示词）已部署到 Railway production 并跑了一次真实任务。本文记录那次实跑用**容器内真实产物**砸出来的 8 个根因 + F1–F6 修复计划。**下个 session 从这里冷启动，直接实现 F1–F6 然后重新部署。**
>
> 日期：2026-05-28 · 部署 SHA：`e56050c` · 分支：main

## 实跑事实

- Agent：`ec03ec3e-c4e8-417d-95f7-f84215e7b9c3`（Web3研究员），模型 deepseek-v4-pro
- Task：`b05c72f1a1d1435aba5b98d8418445a2`，`industry_research` / `full` / 语言 `zh` / max_sources `8` / 10 维度 / 6 lane
- 耗时 ~14 分钟（15:09→15:24），**status=failed**（synthesis 两次 <1200 字）
- 结果：8 source / 35 claim / **5/6 quality gate 过**（attribution/plurality/freshness/completeness/contradiction 全 passed，只 synthesis failed）

## 先记住：哪些已经成功（别回退）

- ✅ **计划门生效**：先出计划卡（中文/参数确认）才开跑；`plan_confirmed` 路由工作。
- ✅ **语言锁定**：全程中文，无混语。
- ✅ **6 worker 并行 + 零崩溃**：新代码（grading/language/prompt_craft + 改写 orchestrator）线上零 traceback。
- ✅ **诚实失败 + 完整留痕**：没出拼接垃圾；写了失败通知 + 保留全部 ledger，所以才能挖到 token 级真相。**这是相对旧版"1 小时拼接怪"的质变，必须保住。**

## 容器内产物证据（artifact 路径见末尾）

worker_reports.jsonl **606KB**、sources.jsonl **107KB**（8 源 ≈13KB/源=二进制膨胀）、report.md 仅 1675B（失败通知）、**无 devils_advocate.jsonl**、无 source_notes.jsonl（P2 已删，符合预期）。

| worker | 抓到源 | digest 长度 | tokens |
|---|---|---|---|
| #1 | 7 | 3657 | 179K |
| #2 | 6 | **81** | 168K |
| #3 | **18** | **33** | **452K** |
| #4 | 9 | 4134 | 203K |
| #5 | 13 | 4449 | 252K |
| #6 | 11 | 4637 | 175K |

合计 **64 源 → ledger 只留 8**（来自 #1 的 7 + #2 的 1，循环填满 8 格即 break）→ lane 只剩 market_data(7)+protocol(1)。8 源**全 tier3**。其中 1 源是 `RWA-Report-2025.pdf` 的**未解析 PDF 二进制**（title=`%PDF-1.4`，content 12001B 全是 `/FlateDecode` 流）；另 1 源 title=`📄 Firecrawl content from`（信封泄漏）。

## 8 个根因（按影响排序 + 代码位置）

| # | 根因 | 证据 | 代码位置 |
|---|---|---|---|
| **RC1** | `max_sources=8` 太小 + 全局贪婪填充，64→8 且挤在前 2 lane | 64 抓到/8 留存 | `orchestrator._run_worker_path` 源接收循环 `if accepted_sources >= request.max_sources: break`；`schemas.ResearchRequest.max_sources`(默认8)+`from_arguments` |
| **RC2** | PDF/二进制未抽文本，原始字节当 content | title=`%PDF-1.4`，content=PDF 流 | `worker._source_from_tool_event`→`extractor.clean_fetched_text`（**无 PDF/二进制防护**）；pdfplumber/pypdf 已装但没用 |
| **RC3** | worker 被巨型内容撑爆：#3 烧 452K token 出 33 字 digest | #2/#3 digest 81/33 | 单页 fetch 结果不限长（worker 在 invoke_agent 循环里吞下巨页）；无 per-worker 源数上限 |
| **RC4** | 合成产出不足 <1200×2 | synthesize 步 failed | `reasoner.build_digest_synthesis_instruction` + `prompt_craft.WRITING_QUALITY`(反凑数偏简) + `orchestrator._minimum_report_chars`(full=1200) |
| **RC5** | 魔鬼代言人返回空，没写盘 | `da_path=None`，无 devils_advocate.jsonl | `reasoner.devils_advocate_review`(JSON 容错/被二进制干扰) + `orchestrator._maybe_devils_advocate` |
| **RC6** | 分级失灵，8 源全 tier3 | 全 tier3 | worker 捕获 `source_type=UNKNOWN` 恒定；`grading.grade_source` 域名启发式没覆盖 rwa.xyz/researchandmarkets |
| **RC7** | lane_summaries 空心(findings=0) | key_findings 全空 | `orchestrator._aggregate_lane_summaries` 从 source_notes 取 findings，P2 删了 source_notes |
| **RC8** | 标题/信封污染 | title=信封/`%PDF` | `worker._extract_title` 抓到 envelope/PDF 头 |

> 注：agent 调 start 时 **没回传 worker_topics**（`confirmed_topics=0`），且确定性 `_worker_topics` 给每个 worker 都前缀了整道 10 维巨题 → worker 不聚焦、烧 token。属 RC3 + 接线问题。

## F1–F6 修复计划（v2.1 硬化，TDD，下个 session 实现）

| # | 修 | 治 | 做法 |
|---|---|---|---|
| **F1** | max_sources 按 depth 提高 + 公平分配 | RC1 | `from_arguments` 默认：quick=6/standard=12/full=30/flagship=40；源接收改**按 worker 轮转/每 lane 保底**，不再先到先占。每 worker 上限 `max(2, max_sources/len(workers))`。 |
| **F2** | PDF→文本抽取 + 弃二进制 + 限长 | RC2 | worker 捕获时检测 `%PDF`/二进制；PDF 用 pdfplumber/pypdf 抽文本（已装）；不可解码二进制弃源；content 清洗后限长(~12K)。 |
| **F3** | 防 token 爆 + worker 聚焦 | RC3 | 每次 fetch 结果在喂回 LLM 前限长；每 worker 捕获源数上限；worker 主题别塞整道巨题(传聚焦 lane 主题)。 |
| **F4** | 合成深度下限 + 软化反凑数 | RC4 | 提示词加"full depth 期望多节充分展开的综合报告"；调和 WRITING_QUALITY 的"别凑数"与长度下限。 |
| **F5** | 覆盖感知兜底(C) | RC4/可用性 | 证据确实有限时出**缩域报告 + 明确未覆盖 lane**，而非整体失败。 |
| **F6** | 收尾增强 | RC5–8 | source_type/tier 分类增强(按域名+内容)；DA JSON 容错(重试/宽松解析)；lane findings 从 worker digest 回填；title 信封/PDF 头清洗。 |

**优先级**：F1+F2+F3 是大头（把"8 个 lopsided 脏源"变成"~30 跨 lane 干净源"，合成自然有料）；F4+F5 保合成出得来；F6 收尾。

## 如何访问线上产物（下个 session 复用）

`railway ssh [COMMAND]` 可在生产容器执行只读命令（已 link backend）。产物路径：
```
/data/agents/<agent_id>/runtime_artifacts/long_tasks/<task_id_hex>/deep_research/
```
读法：把 python 分析脚本 base64 后 `railway ssh "echo <b64> | base64 -d | python3"`（避免引号地狱）。本次用的 inspector 见会话历史（解析 request/sources/worker_reports/lane_summaries/evaluation/final/steps）。backend 域名 `https://backend-production-326d.up.railway.app`。

## 关联
- `docs/DEEP_RESEARCH_V2_DIAGNOSIS.md`（原始代码诊断）
- `docs/DEEP_RESEARCH_V2_FIX_PLAN.md`（v2 批次，已落地+部署）
- 本文 = v2.1 线上实跑发现 + 下一批 F1–F6（待实现）
