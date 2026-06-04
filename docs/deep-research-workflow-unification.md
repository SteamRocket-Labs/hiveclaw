# Deep Research × Workflow 路径统一（单一路径定稿设计）

状态：**设计稿，待拍板**（2026-06-04）
前置：`docs/workflow-source-capability.md` P0–P15 已全部实装（commit `4dea0449`）；`deep_research.v1` definition 已注册但只是入口壳，`WORKFLOW_DEEP_RESEARCH_ENABLED` 默认 false。
本文档是该 flag 翻转并删除、旧路径退役前的唯一权威路线。

---

## 1. 目标与不变量

**用户拍板（2026-06-04）：不留两条路径。Deep Research 的全部能力迁到 workflow 路径，旧 orchestrator 退役，最终只有一条路。**

四条不变量，违反任何一条即设计失败：

| # | 不变量 | 含义 |
|---|--------|------|
| I1 | **单一路径** | 迁移完成后 `deep_research_start` 只走 workflow runtime；linear/controller/worker 三路由、`RuntimeTask(task_type="deep_research")`、flag 本身全部退役。 |
| I2 | **质量不回退** | RC11（合成零工具）/RC12（幻觉引用中和）/RC13（coverage 强制）/RC14（plan gate）/source ledger 分级 全部保真；切换 gate = 真问题实跑对照（§6 切口 5）。 |
| I3 | **治理合规（§6.3）** | leaf 执行核心永远是 `spawn_subagent` —— 不开第二条私有执行路径。DR 的领域能力以「preset 注入 + 确定性前后处理」包在 spawn 外面，治理/租户/审计/预算全部继承。 |
| I4 | **产品面 parity** | 报告落盘（report.md + 多格式导出 + workspace packet）、`deep_research_check` 进度查询、artifact 目录结构 — 用户拿到的东西不少于旧路径。 |

---

## 2. 资产盘点与落点映射

旧路径 ~6,220 行（`app/services/deep_research/`）+ handler 1,073 行。逐资产落点：

| 资产 | 现职责 | 落点 |
|------|--------|------|
| `orchestrator.py` (1263) | linear 路由主循环 + synthesis 门（RC12/RC13 在此） | **拆**：RC12 `_strip_unknown_source_refs`、RC13 `_evaluate_synthesis_quality`、`_apply_footnotes`、`_aggregate_lane_summaries` 下沉到 synthesize leaf 后处理；三路由 dispatch 与 linear 主循环**删** |
| `controller.py` (611) | token 预算动态决策循环 + Beast mode | **删**（预算循环由 workflow quota + `max_tool_rounds` 取代；Beast mode 不保留，见 D4） |
| `worker.py` (312) | 管制 worker：工具白名单 + 真 invoke + source 捕获 | **形态保留** → explorer leaf 的实现蓝本（它已经就是 leaf 该有的样子） |
| `reasoner.py` (921) | disable_tools LLM 推理面（RC11 在 `_invoke:606`）、synthesize_from_digests、devils_advocate | **拆**：synthesis/critic 的提示词与 disable_tools 语义进 synthesizer/critic leaf preset；plan 细化进启动前 handler（现状已是）；其余随 linear 路由删 |
| `ledger.py` + `grading.py` (208) | SourceRecord/ClaimRecord 记账 + tier/grade 启发式 | **保留原样**，消费方式改为 per-leaf 分片 + synthesis 前合并（D2） |
| `searcher.py` / `reader.py` / `extractor.py` / `evaluator.py` | 检索/抓取/确定性 claim 提取/质量门 | **保留**：extractor/evaluator 进 leaf 后处理；searcher/reader 的抓取链由 explorer subagent 经真工具自然替代，保留为后处理的 URL 规范化/清洗工具函数 |
| `writer.py` + `artifact_composer.py` (513) | artifact 落盘 + MD→HTML/DOCX/XLSX/PPTX | **保留原样**，调用点移到 synthesize leaf 后处理与 run 完成 hook（D5） |
| `reflector.py` (144) | 每轮反思决策 | **删**（linear 路由专属；workflow 形态下 explorer 自带 ReAct 反思，fanout 后无逐轮反思点） |
| `planner.py` + `plan_contract.py` + `plan_mode.py` | plan card / RC14 gate / contract 校验 | **不动**（plan 阶段在 workflow 启动前，已统一） |
| `schemas.py` / `language.py` / `routing_reminder.py` | 数据类 / 语言一致性 / 软路由提醒 | **不动** |
| handler `deep_research.py` (1073) | start/run/check/cancel/export + RuntimeTask CRUD + 落盘辅助 | **收缩**：start 只走 workflow；check 改查 workflow run；`run_deep_research` 调用、后台 runner、`RuntimeTask(deep_research)` CRUD 删 |

**workflow 引擎已原生提供、迁移后直接删的职责**：fanout 并发/失败隔离（旧 semaphore）、token 预算池（旧 token_used 手工记账）、journal/resume（旧 steps.jsonl 的恢复职责——日志价值由 leaf journal 取代）、运行记账（旧 RuntimeTask 手工 CRUD）。

---

## 3. 核心设计决策

### D1 — Leaf Preset Registry（本设计的核心抽象）

**问题**：definition 里的 leaf 只有 `{name, type, max_tool_rounds}`，而 DR leaf 需要专业系统提示词、工具白名单、RC11 零工具、以及**确定性前后处理**（记账/中和/质量门——不能依赖 LLM 自觉）。

**决策**：新建系统侧 **leaf preset registry**——`leaf name → LeafPreset{spec_overrides(allowed_tools/excluded_tools/system_prompt/disable_tools), pre_process, post_process}`。

```
LeafRequest → [preset.pre_process: 注入上下文]
            → spawn_subagent(spec ⊕ preset.spec_overrides)   ← 执行核心，I3 合规
            → [preset.post_process: source 捕获→ledger 分片 / RC12 strip / 质量门 / 落盘]
            → LeafOutcome
```

为什么是 registry 而不是把提示词塞进 definition 数据：preset 是**代码**（系统能力），definition 只引用名字；admission 的 leaf catalog binding（`DEEP_RESEARCH_WORKFLOW_LEAVES`）已经是「名字即授权单元」——registry 是它的自然升级，不开放任意 system_prompt 注入面。与 P15 的 `metered_leaf_executor` 同构（wrapper 先例已立）。

实现锚点：`SubagentSpec` 已有全部注入字段（`allowed_tools/excluded_tools/system_prompt/max_tool_rounds`），**唯一缺口** = RC11 的「零工具」语义（`allowed_tools=()` 现在回落到 type 默认集，需要显式 `disable_tools` 直通到 `AgentInvocationRequest.disable_tools`——该字段 RC11 时已建好，只差 spec→request 这一跳）。

### D2 — Ledger 跨 leaf 共享：per-leaf 分片 + synthesis 前合并

旧 ledger 是进程内对象。fanout 的 N 个 explorer 并发执行（max_concurrency=4），共享 append 同一 jsonl 有竞争。

**决策**：每个 explorer leaf 的 post_process 写**自己的分片** `sources-{leaf_id}.jsonl` / `claims-{leaf_id}.jsonl`（无锁、确定性、与 leaf journal 天然对齐）；synthesize leaf 的 pre_process 合并全部分片 → 统一 `grade_source` 复核 → 喂给合成与 RC12/质量门。合并产物落 `sources.jsonl`/`claims.jsonl`（与旧目录结构同名，I4 parity）。

### D3 — plan 细化在 workflow 启动前；definition 的 plan step = strategy brief

v1 引擎决策：`items_from` 只能引用 `args.*`（admission 时固定）。所以 fanout 的 `worker_topics` 必须在启动前定稿——现状已是（plan card 确认时 `deep_research_workflow_args` 算好）。definition 里的 plan step **保留**但语义收窄为 strategy brief：输出 per-topic 检索指引（lane 策略、source policy 展开），fanout 的 `per_item_task` 引用 `{{steps.plan.output}}`（现 definition 已这么写）。旧 `_maybe_refine_plan` 的价值在此下沉。

### D4 — Beast mode 不保留

旧 controller 在 85% 预算时强制降级综合。workflow 哲学相反：**预算不足 = quota suspended = 确定性状态 + 人工决策**，不静默降级。证据不全时如何综合的问题已由 RC13 coverage notice 语义覆盖（narrowed report + 明确缺失 lane）；fanout 失败隔离保证部分 explorer 失败不毁整个 run。运维出路：suspended → 调预算 resume，或带着已有证据人工触发 synthesis（resume 即可——fanout done 的分片都在）。

### D5 — 报告落盘归 synthesize leaf 后处理；check 统一查 workflow run

- synthesize post_process：合并 ledger → 合成 → RC12 strip → `_apply_footnotes` → RC13 质量门 → `report.md` + `final.json` 写 artifact_dir（目录结构与旧路径同构，挂在 workflow run 的 artifacts 下）
- run 完成边界（critic done 后，handler 侧）：`_materialize_requested_output_format`（HTML/DOCX/XLSX/PPTX 导出）+ `_publish_workspace_packet`（workspace 镜像）
- `start_deep_research_workflow_run` 返回真实 `workspace_artifact_dir`（替换现在的 null——I4 的硬验收）
- `deep_research_check` 改为：workflow run journal（步状态/进度）+ artifact_dir 增量读（partial report）。`RuntimeTask(task_type="deep_research")` 不再创建。

### D6 — critic leaf = devils_advocate 下沉

旧 worker path 的 `devils_advocate_review`（对抗评审）即 critic leaf 的实现内核：disable_tools + 对抗提示词，输出结构化批评（coverage/attribution/contradictions/freshness/unsupported）。批评结果写 `devils_advocate.jsonl`（parity）并作为 run 输出的一部分；**不自动回写报告**（批评是给用户/后续 RC15 的输入，不是静默改稿——与「dream 提议不静默改 charter」同一哲学）。

---

## 4. 不迁移的东西（明确刻在这里）

- **RC15（synthesis 论点驱动重构）**：独立后续切口，迁移先保真。迁移后 synthesis 提示词集中在 synthesizer preset 一处，正是 RC15 的理想起点。
- **`resolve-reconciliation` admin 命令**：见 runbook，未排期。
- **routing_reminder / plan_mode / plan_contract / schemas**：不动。

---

## 5. 切口序列（每切口一 commit + red tests + 文档证据，TDD 铁律不变）

| 切口 | 目标 | 改动面 | Red tests（关键断言） | 验收 |
|------|------|--------|----------------------|------|
| **DR-1 preset 地基** ✅ 完成（2026-06-04） | Leaf Preset Registry + disable_tools 直通 | 通用层落 `services/workflow_leaf_presets.py`（LeafPreset + register/resolve/reset，process-global registry——daemon resume 按 leaf name 自恢复）；`SubagentSpec.disable_tools` 新字段 → spawn 的 `AgentInvocationRequest.disable_tools`（RC11 字段早已存在，补上 spec→request 一跳）；`workflow_launch.build_subagent_leaf_executor` 查 registry：spec overrides 合入 + pre_process 改写 task + post_process 变换 outcome | ✅ 5 个 red→green：preset overrides 真进 spawn / disable_tools 进 spec / 无 preset leaf 零变化（office 断言）/ pre 改 task + post 变换 outcome / spec.disable_tools→request（tests/agents/test_subagent.py） | ✅ 全量 **3711 passed, 7 skipped**（基线 3706 +5）；ruff clean；office workflow 测试零变化 |
| **DR-2 explorer leaf** | worker.py 形态迁入 preset | explorer preset：web 工具白名单（沿用 `RESEARCH_WORKER_ALLOWED_TOOLS`）+ source 捕获 post_process → ledger 分片 + 确定性 claim 提取（extractor） | 真 PG + fake spawn：explorer leaf 跑完产出 `sources-{leaf_id}.jsonl` 分片、URL 规范化去重、tier/grade 已标 | fanout 跑 2 topic → 2 份分片，互不竞争 |
| **DR-3 synthesize + critic leaf** | RC11/12/13 + 落盘下沉 | synthesizer preset（disable_tools + 合成提示词）+ post_process（合并分片→合成→RC12 strip→footnotes→RC13 门→report.md/final.json）；critic preset（devils_advocate） | 含幻觉 src 引用的合成被中和（RC12 锚测试迁移）；缺 lane 时 coverage notice 出现（RC13）；synthesizer spawn 的 request.disable_tools=True（RC11）；report.md 真落 artifact_dir | 全链 workflow run（fake spawn 喂语料）completed + 报告文件存在 |
| **DR-4 产品面 parity** | check/导出/workspace 统一 | handler：start 返回真实 workspace_artifact_dir；run 完成边界做导出 + workspace packet；`deep_research_check` 改查 workflow run + artifact 增量；删 `RuntimeTask(deep_research)` 创建 | check 对 workflow run 返回 步进度+partial；docx 导出文件真生成；workspace packet 镜像齐 | 旧 check 测试改写后全绿 |
| **DR-5 实跑切换 gate** | 真问题全链验证（I2 的 gate） | flag 翻 true（env），生产 repro 模式实跑 ≥1 个真实研究问题 | — | 对照旧路径报告：来源数/引用完整性/coverage/落盘文件清单 不回退；实跑证据写入本文档 |
| **DR-6 旧路径退役** | I1 兑现 | 删 orchestrator 三路由+linear 主循环、controller.py、reflector.py、handler 的 run_deep_research 调用与后台 runner、`WORKFLOW_DEEP_RESEARCH_ENABLED` flag 本身；旧路由测试删/改写 | 退役后全量绿；grep 无 `deep_research_workflow_enabled`、无 `task_type="deep_research"` 创建点 | `deep_research_start` 唯一路径 = workflow；文档全段收尾 |

回退方案：DR-5 之前 flag 默认 false 随时停；DR-6 是不可逆点（删除），其 gate 就是 DR-5 的实跑证据——**没有实跑对照证据不进 DR-6**。

---

## 6. 风险清单

| 风险 | 缓解 |
|------|------|
| explorer subagent 的检索质量 ≠ 旧 searcher/reader 链（jina→web_fetch→firecrawl 降级链是手工编排的） | explorer preset 的 system_prompt 写明抓取降级策略；post_process 复用 reader 的清洗函数；DR-5 实跑对照来源质量 |
| fanout 分片合并后 source_id 跨分片冲突 | source_id 生成含 leaf_id 前缀（确定性，无协调） |
| synthesis 输入超 context（旧 digest-based synthesis 解决的问题） | 沿用 `synthesize_from_digests` 的分批策略进 synthesizer post_process（输入是合并后的 lane digest 而非原文堆） |
| spawn 的 subagent 看不到 artifact_dir（写盘归属混乱） | **leaf 内 LLM 永不写盘**——全部落盘在 post_process（系统侧），LLM 只产文本输出 |
| DR-6 删除后发现新路径缺口 | DR-5 实跑 gate + revert 单 commit 即回 flag 共存态 |
