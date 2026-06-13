# 外部行为 Eval CI 设计（Candidate Canonical）

> **文档定位**：这是 Hive 第二轮第四仗（可观测）的**候选收口设计**，也是兑现 `docs/round2-sota-benchmark-2026.md` §9 铁律「**做完外部行为 eval 前，不宣称"已超越"**」的工程方案。第一轮把 Hive 对标 CC 做了 harness 审计；第二轮五仗实装了 18 个 milestone；但所有"超越"目前只有**service-level 自进化检查 + 注入/源码回退的 Hermes 分数**背书，不是**agent-core live 行为级**证据。这份文档把"外部行为评估"从一次性脚本，变成 agent **改不动**、能 **block merge**、能**门控自进化晋升**、能**检测退化**的硬验证闭环。
>
> **单一核心（贯穿全文，每个决策都回到这句）**：
> **让一个 agent 无法改写的外部硬验证器，对每一次改动与每一次自进化晋升，证明「真跑了任务、真达标了、相对上一基线没退化」——否则不准合并、不准晋升。**
>
> **这不是从零造**：行为评估骨架 `backend/app/evals/bakeoff_runtime.py` 已具备 live CLI 路径（shell-out 到 `claude`/`hermes` CLI、临时工作区、外部硬判据评分），但当前仍允许 `repo_evidence` fallback，且只接到统一 runner、未接 CI fail-gate / `evolution_ledger` / promotion gate。本设计 = **硬化 live runner + 接通 ledger/promotion + 补齐 10 个 gap + 防 reward-hack**。
>
> **交付纪律**：按仓库 CLAUDE.md「一次改完、零债」——本文定义**完整端到端 scope**（接通 + 硬化 + 双层 CI + 门控 + 隔离 + 多轮 + 防作弊），不做"Phase 0 先上、later 补门控"的 MVP 切法。唯一例外是不可逆生产步（基线快照首次落库）用 dry-run + 确认门，那是安全门不是 MVP 阶段。
>
> **诚实纪律**：区分 **Fact**（读代码/读文档核实）/ **Inference**（证据推断）/ **Decision-pending**（待 owner 拍板）。本文不把"代码存在"当"生产活着"——已存在但未硬门化的 runner 就是反例。

## 修订原因（2026-06-13）

本版把上一稿中几处过度乐观或不精确的 Fact 改成当前代码真实状态：

1. `bakeoff_runtime.py` 已被 `backend/app/evals/run.py` 的 bakeoff mode 调用，但没有进 CI，也没有 fail-gate、ledger 或 promotion gate；因此不能再写成"从未被 run.py 调用"。
2. `bakeoff_runtime.py` 有 live CLI 路径，但 CLI 缺失、auth/preflight 失败或部分场景失败时会回退到 `repo_evidence`；硬门设计必须让 fallback fail-closed 或显式 `unavailable`，不能把源码证据当 live 行为分。
3. `self_evolution_bakeoff.py` 的 Hive 侧已经不是纯源码 marker，而是 service-level 行为检查；但它仍不是 `invoke_agent()` 级 agent-core live eval。为了避免低估已完成工作、同时避免高估行为证据，本文改用 "service-level 自进化检查"。
4. 仅靠 git-tracked hash 不能证明 evaluator 隔离；如果 grader 和 expected hash 在同一个 PR 中都可改，hash 校验会被一起更新。可信设计需要受保护基线源、CODEOWNERS/required review，或 CI 从 protected base branch 取 evaluator/baseline。

---

## 0. 在五仗路线里的坐标

| 锚点 | 现役语义 | 本文关系 |
|---|---|---|
| round2 §9 Eval 体系 | 立铁律：行为级双系统 bakeoff + 外部可验证 reward 子集 + 持续 eval CI + 退化检测；做完前不宣称超越 | **本文定义 §9 的实装方案** |
| round2 §10 总表 #12 可观测/eval | trace/feedback 地基已接（invocation_spans + Session Useful/Misleading）；**仍缺外部行为 eval CI** | 本文定义关闭这个"仍缺"的完整方案 |
| round2 §11 结构性落后 #4 | 可观测：trace/反馈地基已关闭；**无外部行为 eval CI 是下一仗硬缺口** | 本文定义关闭方案 |
| round2 §12 第四仗 | 可观测先行（O1：invocation_spans + reader + Prometheus 已实装） | 本文是第四仗的延伸收口 |
| self-evolution-plan P0 | "self-improvement cannot start until the system can observe, score, and roll back behavior"；red test 含 `tests/evals/test_bakeoff_runtime.py` | 本文定义让 P0 的 eval 地基真正承重的实现路径 |
| self-evolution-plan P3 | verification-gated promotion（candidate→eval→promotion） | 本文定义把"行为 eval 结果"接进 P3 eval 步的路径 |
| self-evolution-plan P7 | "Hive vs Hermes bakeoff" 标记完成 2026-05-24，**实为 ~30%**（service-level，Hermes 从未真跑） | 本文定义把 P7 真正做完的路径 |

**一句话**：第二轮的"超越"叙事，全靠这一仗补上**行为级证据**才成立。没有它，北极星 Goal-1「至少和 hermes 一样好」是**未验证 Speculation**。

---

## 1. 现状坐标（baseline，诚实摆出来）

### 1.1 两条 eval 线 + 关键断裂

**线 A — `self_evolution_bakeoff.py`（service-level 自进化检查，已接 CI）** — *Fact*
- 6 个固定场景（`next_turn_adaptation` / `repeated_workflow_learning` / `tool_failure_lesson_reuse` / `skill_candidate_creation` / `long_task_resume` / `safety_tenant_policy`，line 19-74）。
- `_score_hive`（line 111）直接调 Hive 的 **service 函数**（`create_fast_reflection_candidate`、`record_skill_execution`、`scan_skill_files`…），检查返回结构 / 文件存在性 / 字段值。**这是 service-level 行为/结构检查，不是 `invoke_agent()` 驱动的 agent-core live run。**
- Hermes 分数：要么 `--hermes-scores-json` **硬编码注入**（CI line 60），要么 `_derive_hermes_scores`（line 479）**grep hermes 源码里的关键词 marker 计数**。**两条都不是 Hermes 真跑——是数代码里有没有某些字符串。**（审计 P1-13 已实锤"92 vs 85 是源码字符串存在性"。）
- `main()`（line 658）`return 0 if passed else 1`——它**会** fail CI，但门控的是 service-level 自进化检查 + Hermes 注入/源码回退分，不是 agent-core live 行为。

**修订原因**：`docs/harness-engineering-audit-2026-06-11.md` 已记录 Hive 侧从旧 marker 检查升级为 `local_behavior_scenarios`，所以继续称它为"静态代码打分"不准确；但它仍没有让完整 agent loop 在真实任务上运行，不能当 §9 的外部行为证据。

**线 B — `bakeoff_runtime.py`（live CLI 行为骨架，未硬化）** — *Fact*
- 8 个场景（`coding` / `review` / `research` / `operations` / `delegation` / `memory_recall` / `self_evolution` / `long_context_after_compaction`，line 17-26）。
- live path 下，`run_runtime_bakeoff`（line 593）**真 shell-out** 到 `claude` / `hermes` CLI（`build_runtime_command` line 91），每个场景配**真实工作区文件**（TASK.md + 待修代码，`_scenario_workspace` line 283），外部 agent CLI 真跑。
- `_score_runtime_scenario`（line 394）用**外部硬判据**评分：`coding` 查 `calculator.py` 是否含 `return a + b`；`memory_recall` 查答案是否含 `cedar-lantern`；`long_context_after_compaction` 查是否捞出 `delta-saffron-42`。**这正是外部行为 eval 应有的样子。**
- **硬化缺口**：CLI 不存在、auth/preflight 失败时会走 `repo_evidence_only`；部分场景失败时会替换为 `repo_evidence_fallback`。这些 fallback 可用于诊断，**不能用于 merge/promotion 硬门给分**。
- **断裂点**：`run_runtime_bakeoff` 已接 `backend/app/evals/run.py` 的 bakeoff mode，但**未接 CI、未接 fail-gate、未接 `evolution_ledger`、未接 promotion 逻辑**；`backend/app/evals/run.py` 的 CLI 当前也不按 score/pass_rate 失败退出。

**修订原因**：当前代码确实存在 live CLI runner，但测试也明确覆盖 auth missing 时 fallback 到 repo evidence。文档必须把 "live runner 骨架已存在" 和 "硬验证闭环未成立" 分开，否则会把可诊断分数误当可门控分数。

### 1.2 evolution_ledger 链已实装，但 eval 步接不到行为 — *Fact*
- `record_evolution_candidate`(line 52) → `record_eval_run`(line 144, 字段 `reward`/`passed`/`critical_regressions`/`traces`) → `record_promotion_decision`(line 203)。链是全的。
- `run_evolution_verification`(`evolution_verification.py` line 363) 跑 grader 列表：`deterministic_command` / `state_check` / `tool_call_check` / `skill_guard`(line 294，第一仗 M1 已升为硬门) / `llm_rubric`(**占位**) / `human_confirmation`。
- **缺口**：所有 grader 都是文件系统级 / 结构级 / 静态规则级。**没有一个 grader 会"让 agent 真跑一个任务、用外部判据看它干成没有"**。线 B 的行为结果没有通道进 `record_eval_run`；`decide_verified_promotion()` 当前也只看 `verification_report.passed`，不看行为回归或基线退化。

### 1.3 Vercel Sandbox 的真实定位（纠正上一 session 的设想）— *Fact + Inference*
上一 session 的 next-step 设想"用 Vercel Sandbox microVM 当 agent eval runner"。**调研纠正了它**：
- microVM **不能**跑完整 agent 循环——`invoke_agent()` 需要 DB（`Agent.load`/`User.load`/`FeatureFlag`）+ LLM API（Anthropic）；microVM 默认 `deny-all` 断网、env 过滤掉 `DATABASE_URL`/API key。`allow-all` 又破坏隔离承诺。
- microVM **能**且**应**承担两个角色：
  1. **产物执行验证器**（Voyager 入库前 gate）：agent 产出的 skill/代码候选，在 microVM 真跑、检查产物满足声明。`services/code_execution/`（`service.py` provider factory + `vercel_provider.py`）已具备 workspace tar 双向同步 + 超时 + `CodeExecutionResult(stdout/stderr/exit_code/timed_out)`。
  2. **被测 agent 的工具执行隔离层**：agent 在主 backend 跑（脑），其工具/代码执行落 microVM（手），副作用不污染生产。

> **架构定论**：**agent-core 在主 backend（需 DB+LLM），evaluator/产物执行在 microVM（隔离）**。这是 split architecture，不是"把 agent 塞进 sandbox"。

### 1.4 10 个 gap（来自 2026-06-13 eval 基础设施深度审计）— *Fact*

| # | Gap | 现状 | 北极星依据 |
|---|---|---|---|
| G0 | 无 Hive agent-core live 行为 runner | `bakeoff_runtime` 只跑外部 CLI；`self_evolution_bakeoff` 跑 service-level 检查，不跑 `invoke_agent()` | Goal-1 行为证据 |
| G1 | 无 live 行为 eval 硬门入 CI | 线 B 已接统一 runner，但不在 CI；runner CLI 也不按回归失败退出 | §9-1 行为级双系统 |
| G2 | 无硬化基线快照 | Hermes 单行 JSON 注入，无版本/模型/日期/git tag | §9 铁律"非 fixture" |
| G3 | CI 无 regression fail-gate | 线 B 不 block merge | §9-3 退化检测 |
| G4 | 无 reward-hack 防御 | agent 能改 verification 代码；ledger append-only 非 immutable；无受保护 evaluator/baseline 信任源 | §2.3 铁律2（验证器在可改写面之外）、DGM Node 114 |
| G5 | 无置信度/连续分 | reward 硬编码 0.0/1.0 | §2.3（硬度可量化） |
| G6 | 无 per-scenario 成本强制 | 只查 guard 存在性 | §10 #8 cache 经济 |
| G7 | 无基线模型版本追踪 | Claude 模型升级基线静默漂移 | §9 铁律（基线可复现） |
| G8 | 无多轮 variance | 单次快照，无 N-run 稳定性 | §2.3（退化是时序现象） |
| G9 | 无 evaluator 隔离 | grader 代码在 `app/services/`，自进化时可被改 | §2.3 铁律2 |

---

## 2. 不可谈判的铁律（设计宪法，来自 round2 §2.3 + §9）

任何实现细节与这四条冲突，以这四条为准：

1. **验证器外部且硬。** pass/fail 的唯一决定者是**硬可验证信号**（执行成败、单测、确定性命令、产物存在性/正确性、状态检查、外部 ground-truth）。LLM 自评（rubric）只能当**软补充**（质量分、诊断、解释），**永不当 promotion / merge-gate 的最终裁决**。依据：Self-Rewarding 啰嗦漂移（1092→2552 字符）、R-Zero 软投票退化（79%→63%）。
2. **验证器与审计在 agent 可改写面之外（架构隔离）。** agent 不能改自己的 grader 代码、不能改基线快照、不能改 ledger。依据：DGM Node 114 删自己的幻觉检测标记刷满分、STOP 改 `use_sandbox=False`。**能力越强，玩弄验证信号的倾向越强**（GPT-4 禁沙箱尝试率 >2× GPT-3.5）。
3. **进化 lineage 是一等审计对象。** 每个 candidate→eval(带 traces)→promotion 全程可追、可回滚。DGM 的 archive 谱系正是抓到作弊的唯一手段。
4. **不退化是硬门，基线可复现。** 每次 eval 对比版本化基线快照（带 model/date/commit）；模型变更时基线显式重跑 + 标注，不静默漂移。

> **reward-hack 对抗测试是验收的一等公民**：喂"删了检测标记的坏技能""谎称测试通过的候选""改 grader 的提名"，必须被拦。一个不能被自己的 agent 攻破的 eval，才是 eval。

---

## 3. 三个待决决策 → 推荐 + 理由（待 owner 拍板）

> 按"先文档后拍板"，这里给**推荐方案 + 理由 + 权衡**，请在文档上 confirm 或 override，不抛选择题。三个决策互相耦合，推荐方案是一个自洽组合。

### 决策 1 — 基线来源：Hive 历史自比 vs Hermes 横向对比

**推荐：两者都要，角色不同 —— Hive 版本化快照当 regression 主力（per-PR 硬门），Hermes 真跑当 quality bar 标尺（nightly/里程碑）。**

- **Hive 自比快照 = 不退化的硬门。** 同一套行为任务、同一套硬判据，对比"上一个 promoted 基线"，纯 delta。这是 §2.3 铁律4 最干净的不退化信号，且不依赖外部 CLI，必须 per-PR 卡。
- **Hermes 横向 = Goal-1 质量 bar 验证。** 真 shell-out 跑 hermes CLI 同一套任务（线 B 已具备 live path），确认"至少和 hermes 一样好"。但真跑依赖 hermes CLI 可用 + 成本 + 时延，**不适合每 PR**，降为 nightly/里程碑。
- **权衡**：只做自比 → 会"自己跟自己比都达标，但其实整体不如 hermes"（北极星 bar 落空）；只做 Hermes → 每 PR 跑外部 CLI 太贵且 flaky。组合解两难。
- **关键纪律**：Hermes 分数**必须真跑产生**，禁止再用注入 JSON / grep marker（消灭 §1.1 线 A 的假对比）。Hermes CLI 不可用时，**标记 `hermes_baseline: unavailable` 并跳过横向门**，绝不 fallback 成 grep marker 假分。

### 决策 2 — 频率：per-PR vs nightly

**推荐：双层 CI。per-PR 跑确定性硬判据子集（快、block merge）；nightly 跑全量（多轮 + Hermes 横向 + rubric 观察）。**

- **per-PR 层（确定性、便宜、必过）**：跑 ground-truth 明确的行为场景子集（`coding`/`review`/`research`/`operations`/`memory_recall`/`long_context`——这些判据是字符串/文件确定性检查）+ 自进化候选的产物执行 gate。**regression → exit 1 → block merge**。单轮（N=1）控时延。
- **nightly 层（全量、稳、含软信号）**：8 场景全量 + Hermes 真跑横向 + 多轮 variance（N≥3）+ LLM rubric 观察层 + 成本/时延时序。结果写基线时序 + 退化告警，不 block 但产可观测仪表。
- **权衡**：全套塞每 PR → agent 真跑有 LLM 时延（每场景数十秒~分钟级）+ 成本，PR 体验崩；只 nightly → 退化要等一晚才发现、且不挡合并。双层让"确定性不退化"即时挡、"全量质量"持续盯。
- **触发增强**：自进化晋升提名（candidate）**无论 per-PR 还是运行时**，都必须过产物执行 gate 才进 promotion——这是事件触发，不只 PR 触发。

### 决策 3 — 权重：硬判据 vs LLM rubric

**推荐：硬判据 100% 门控；LLM rubric 0% 门控（仅观察/告警）。** 这其实被 §2.3 铁律1 锁死，不是自由参数。

- **硬判据 = 唯一 pass/fail 决定者**：执行成败、单测、确定性命令、产物检查、状态检查。reward = 归一化行为分（连续，解 G5），但 merge/promotion 门只看硬判据布尔结果。
- **LLM rubric = 软补充**：产质量分（0-100）、诊断"为什么弱"、可读解释，写进报告 + 时序，**门控权重 0**。明确标注"非门控"。用满血模型（AI-Native L1：智能步给够视野和预算），但**永不让它决定能不能合并/晋升**。
- **权衡**：让 rubric 参与门控 → 必触发 Self-Rewarding 啰嗦漂移 / 裁判饱和 / reward-hack；完全不要 rubric → 丢了"质量为什么变化"的诊断信号。0% 门控 + 100% 观察 两全。

> **三决策的自洽组合**：版本化自比快照（D1）在双层 CI 的 per-PR 层（D2）用 100% 硬判据（D3）卡不退化；Hermes 真跑 + rubric 在 nightly 层当 bar 标尺 + 质量诊断，全程不让软信号触碰 merge gate。

---

## 4. 目标架构（端到端闭环）

### 4.1 数据流

```
[行为任务集]  ── 固定 + 版本化，每个任务带「外部硬判据」+ 可选 rubric
     │
     ▼
[Agent 真跑]  invoke_agent() / 外部 CLI（Hermes 横向）
     │         脑在主 backend（DB + LLM），手（工具/代码执行）落 microVM
     │         产出：行为轨迹 + 产物（文件 / 代码 / skill / 记忆写入）
     ▼
[外部硬验证器]  ── 在 agent 改不到的可信 CI 环境 / microVM 跑
     ├─ 硬判据 grader：确定性命令 / 单测 / 产物存在性·正确性 / 状态检查   → 门控
     ├─ 产物执行 gate：agent 产出的 skill/代码在 microVM 真跑（Voyager）   → 门控
     └─ LLM rubric：质量分 + 诊断（满血模型）                            → 仅观察
     ▼
[评分 + 基线对比]
     ├─ reward = 归一化硬判据分（连续，G5）
     ├─ 对比版本化基线快照（model/date/commit/variance，G2/G7/G8）
     ├─ regression 检测：任一场景低于基线阈值 → fail（G3）
     └─ reward-hack 检测：protected grader hash/source + evaluator 白名单（G4/G9）
     ▼
[record_eval_run]  ── 接进 evolution_ledger（G1 断裂修复）
     │  reward / passed / critical_regressions / traces
     ▼
[promotion gate]  ── 晋升加硬条件：execution_passed ∧ no_regressions（P3 接通）
     ▼
[CI fail-gate]  ── per-PR regression → exit 1 → block merge（G3）
                   nightly → 基线时序 + 退化告警 + 质量诊断
```

### 4.2 Evaluator 隔离（G4/G9 — 最关键的反 reward-hack 工程）

agent 在自进化时**能写 workspace**。若 grader 代码或基线在 agent 可写面内，就是 DGM Node 114 的企业翻版。隔离三层：

1. **grader 代码不在 agent workspace，且来自受保护信任源**：grader 逻辑只在 `backend/app/evals/`（仓库受版本控制、agent 工具写不到的路径），CI 必须把待测 PR 中的 grader hash 与**受保护 base branch / signed artifact / CODEOWNERS required review**认可的 hash 比对。仅把 expected hash 放在同一个 PR 里不算隔离，因为攻击者可以同时改 grader 和 hash。
2. **基线快照只读 + 显式晋升 + 受保护来源**：基线 artifact 是 git-tracked JSON，但 merge/promotion gate 只能读取 protected baseline；新基线晋升走独立的 promote 步（人/CI 决策 + ledger 审计），agent 路径无写权，普通功能 PR 不得顺手改 baseline。
3. **产物执行在 microVM**：agent 产出的代码候选在 Vercel Sandbox 真跑（`deny-all` 默认断网 + env 过滤 + 独立内核），副作用隔离；grader 的**判分逻辑在 microVM 外**的可信环境，只消费 microVM 返回的 `CodeExecutionResult`。

**修订原因**：hash 校验只有在"期望 hash 的来源不可被同一候选修改"时才有意义。否则 reward-hacker 可以把 grader 和 expected hash 一起更新，CI 仍绿。

### 4.3 基线快照硬化（G2/G7/G8）

基线 = 版本化 artifact（建议 `backend/app/evals/baselines/<suite>.json`，git-tracked）：

```json
{
  "schema": "behavior_eval_baseline.v1",
  "suite": "core_behavior_v1",
  "baseline_version": "1.0.0",
  "baseline_model": "claude-opus-4-8",
  "baseline_date": "2026-06-13",
  "commit_sha": "....",
  "scenarios": {
    "coding": { "score_p50": 100, "score_p95_variance": 0, "transport": "live" }
  },
  "hermes_reference": { "status": "live|unavailable", "scenarios": { } }
}
```

- regression gate：`当前 score_p50 ≥ 基线 score_p50 − 容差`，逐场景。
- 模型变更（`baseline_model` 不匹配运行 model）→ 强制显式重基线 + 标注，不静默比较（G7）。
- 多轮：per-PR N=1（只读 p50），nightly N≥3 写 `score_p95_variance`，variance 爆炸（不稳定）也算回归信号（G8）。

### 4.4 live 行为 runner → ledger → promotion（G0/G1 修复）

- 新增 Hive agent-core live runner：对专用 eval tenant/workspace 启动 `invoke_agent()` / `RuntimeTask`，让 Hive 自己在真实任务工作区内完成任务，并用外部硬判据评分。
- 新增适配层：Hive live runner 与 `run_runtime_bakeoff` 的 per-scenario 结果 → 转 `record_eval_run(reward, passed, critical_regressions, traces)`；只有 `transport=live_cli|hive_live_agent` 且 `benchmark_complete=true` 的结果可参与硬门。
- 新增 grader 类型 `agent_behavior_check`（进 `evolution_verification.py` 的 grader 分派）：输入候选 + 任务，跑 agent 行为 + 硬判据，返回 checks。
- promotion 决策（`record_promotion_decision` 上游）加硬条件：`execution_passed ∧ no_regressions` 才允许 `promote`，否则 `hold`/`reject`。**自进化候选晋升从此必过行为 eval，不只静态 skill_guard。**

**修订原因**：当前 `bakeoff_runtime` 只跑外部 CLI，不能替代 Hive 自己的 agent-core 行为证据；同时 fallback 结果不能写成通过的 hard-gate eval。

---

## 5. 完整交付范围（一次改完，无 MVP）

> 每项 = 改动面 + Red test（TDD）+ 验收。**不留"later 补"的债**。顺序是依赖序，不是分期。

| # | 工件 | 改动面 | Red test（先写失败） | 验收 |
|---|---|---|---|---|
| **E1** | 基线快照硬化 | 新 `behavior_eval_baseline.v1` + `baselines/` git 工件 + protected baseline 加载/对比/模型校验 | 基线含 model/date/commit；模型不匹配强制重基线；缺基线 fail-closed；普通 PR 改 baseline 被拦 | 跑 eval 产生可复现基线；模型漂移被逮 |
| **E2** | live 行为结果接通 ledger | Hive live runner + `run_runtime_bakeoff` live 结果 → `record_eval_run`；新 grader `agent_behavior_check` | 行为结果写出 `eval_run` 事件带 traces；fallback transport 不可 passed；grader 分派命中 | ledger 出现真行为 eval_run |
| **E3** | promotion 硬门 | promotion 决策加 `execution_passed ∧ no_regressions` | 退化候选被 `hold`/`reject`；达标候选 `promote` | 自进化候选必过行为 eval |
| **E4** | 连续 reward | reward 从 0/1 → 归一化行为分；门控仍只看硬布尔 | reward 是连续值；rubric 不改门控结果 | G5 关闭 |
| **E5** | Evaluator 隔离 | grader 代码 hash 校验 + protected expected hash/baseline + 白名单 | 同 PR 改 grader/hash 不被信任；agent 路径无写权；未授权改 baseline 被拦 | reward-hack 对抗：改 grader 被拦 |
| **E6** | 产物执行 gate | 候选 skill/代码在 Vercel microVM 真跑 + 产物校验 | 坏技能（删检测标记/假通过）microVM 跑出失败 → 拦 | Voyager 入库前 gate 生效 |
| **E7** | Hermes 真跑横向 | nightly 真 shell-out hermes；不可用标 `unavailable` 跳过，禁 grep marker | Hermes 分来自真跑；CLI 缺失不 fallback 假分 | 消灭 §1.1 假对比 |
| **E8** | 双层 CI + fail-gate | per-PR 确定性子集 block merge；`app.evals.run` 增加 fail-on-regression/require-live 退出语义；nightly 全量 + variance + rubric + 告警 | per-PR regression 或 required-live fallback → exit 1；nightly 写时序 | G1/G3 关闭，退化即时挡 |
| **E9** | reward-hack 对抗套件 | 坏技能/假通过/改 grader/改基线 四类红队用例 | 四类全被拦截 | 铁律2 可证 |
| **E10** | 成本/时延强制 | per-scenario token/时延预算 + nightly 时序 | 超预算场景告警 | G6 关闭 |

**明确不留的债**：
- 不做"先接 CI、later 补隔离"——E5/E6 与 E2/E8 同批交付（隔离不到位的 eval 是可被作弊的假门，比没有更危险）。
- 不做"先 binary、later 连续"——E4 一次到位。
- 不保留任何注入 JSON / grep marker 的 Hermes 假分路径参与门控——E7 直接删除 `_derive_hermes_scores` 的 marker fallback（或降级为 `unavailable`）；历史报告可保留字段，但必须标记为非门控诊断。

**唯一安全门例外**：基线快照**首次落库**与每次**重基线**用 dry-run 打印 diff + 确认门（人/CI 显式批准）才写，因为它是"定义什么叫达标"的不可逆锚点。这是安全门，不是 MVP 阶段。

---

## 6. 与现有契约的对齐（不破坏什么）

- **evolution_ledger / manifest 契约**：E2/E3 只**新增** eval_run 来源与 promotion 硬条件，不改 `record_*` 既有签名语义；candidate→eval→promotion 仍是唯一审计链（铁律3）。
- **多租户隔离**：行为 eval 任务集是平台级 fixture，不含租户数据；agent 真跑用专用 eval 租户/workspace，产物不回流生产；microVM env 过滤保证 host secret 不上传（对齐 §8 隔离）。
- **AI-Native 三层**：L1 — rubric/任务设计用满血模型给够视野预算；硬判据是确定性的（无需智能，不违 L1，因为它是"机械兜底/客观裁决"而非"智能步降级"）。L2 — evaluator harness 约束在 agent 可改写面之外，界定"能不能晋升"，不替代"想得好不好"。L3 — 任务集 + 判据对任何 model 公平，Hermes 是横向标尺非 vendor 门，模型平等。
- **self-evolution-plan P0/P3/P7**：本设计目标是让 P0 的 eval 地基真正承重、把行为结果接进 P3 的 eval 步、把 P7 从 ~30% 做到 100%。

---

## 7. 风险、反例与验收

**风险**
1. **行为 eval flaky**（LLM 随机性）→ 多轮 variance + 容差阈值（E1/E8），per-PR 用确定性子集降 flake。
2. **Hermes CLI 在 CI 不可用**（auth/keychain）→ `unavailable` 跳过横向门，绝不 fallback 假分（E7）；横向对比只在能真跑的环境当门。
3. **agent 真跑成本/时延**→ 双层切分（D2）+ per-scenario 预算（E10）；per-PR 只跑确定性子集。
4. **microVM 产物执行不能访问 DB/LLM**→ 设计已规避：microVM 只跑**产物代码**（自包含），不跑 agent 循环（§1.3 定论）。

**反例（验收必须证伪的"假完成"）**
- ❌ "接了 CI 但 grader/hash/baseline 可在同一 PR 被改" → E5 protected source 校验未过即假完成。
- ❌ "Hermes 分还是注入的" → E7 未删 marker fallback 或仍让注入分参与门控即假完成。
- ❌ "rubric 偷偷参与门控" → E4/D3 验收：rubric 改不动 pass/fail。
- ❌ "passed=false 但 CI 仍绿" → E8 验收：per-PR regression 真 exit 1。
- ❌ "绿测试 = 完成" → 按 `feedback_green_tests_dont_mean_done`：必须验**生产入口真接线**（CI workflow 真跑、ledger 真出 eval_run、promotion 真被门控），不是测试 pin 未硬门化 runner。

**整体验收（兑现北极星 §9 铁律）**
- per-PR：制造一个真退化（如改坏 compaction）→ 行为 eval 逮到 → CI 红 → 挡合并。
- nightly：Hermes 真跑出分 → Hive vs Hermes 行为级 delta 入时序仪表。
- reward-hack 套件：四类红队用例全被拦。
- **达成后**，方可在 round2 §10/§11 把"仍缺外部行为 eval CI"改为已关闭——**在此之前，"已超越 hermes" 仍是未验证 Speculation。**

---

## 8. 实装进度与证据（Implementation Log）

> 每完成一个 E 即 append 带 TDD red/green 证据。本节是"哪些已**硬门化**"的真相，与 §5 的计划表互补（计划 vs 已落地）。

### E1 — 基线快照硬化 ✅（2026-06-13）

**完成范围**：新增 `backend/app/evals/baseline.py`（functional core，零 DB/LLM）——
- `behavior_eval_baseline.v1` schema + `validate_baseline()`（必填 schema/suite/version/**model/date/commit_sha**/scenarios，逐场景要求 `score_p50`）。
- `load_baseline()` **fail-closed**：缺文件 / 不可读 / schema 非法一律 `raise BaselineUnavailableError`，绝不静默放行。
- `check_model_match()`：运行 model ≠ 基线 model → `raise BaselineModelMismatchError`（G7 静默漂移门）。
- `compare_to_baseline()`：逐场景 `current < baseline_p50 − tolerance` 判退化；基线有而本次缺的场景计入 `missing_scenarios` 并 fail（不能为没跑的场景证明"没退化"）。
- 新增 git 工件 `backend/app/evals/baselines/core_behavior_v1.json`——provisional seed（6 场景，`provisional:true`，分数占位待 E2 真跑回填 + 显式重基线）。

**TDD red**（实现前）：`tests/evals/test_behavior_eval_baseline.py`（12 用例）→ `ModuleNotFoundError: No module named 'app.evals.baseline'`。

**green**：`12 passed in 0.02s`；`ruff check` All checks passed。

**验收映射**：缺基线 fail-closed（`test_load_baseline_missing/invalid_is_fail_closed`）✓；模型漂移被逮（`test_check_model_match_raises_on_drift`）✓；基线含 model/date/commit（schema 必填 + `test_seed_baseline_artifact_is_valid`）✓；regression + 容差（`test_compare_detects_regression` / `test_compare_passes_within_tolerance`）✓。

**gap 状态**：G2（基线快照机制）+ G7（模型版本校验）机制已落；G8（多轮 variance）schema 已留 `score_p95_variance` 字段，nightly 写入留 E8。

**仍待后续 E**：protected 信任源（防普通 PR 顺手改 baseline，§4.2）= **E5**；seed 分数回填 = **E2** 真跑后显式重基线。

### E2 — Hive agent-core live runner + ledger 接通 + grader ✅（2026-06-13，G0/G1）

**完成范围**：
- 新增 `backend/app/evals/hive_live_runner.py`：
  - `run_hive_behavior_eval(agent_runner, output_dir, scenarios)`——驱动 Hive **自己的 agent**（注入 `agent_runner`）跑确定性行为场景（默认 6 个，与 E1 baseline suite 对齐），复用 `bakeoff_runtime` 的真实工作区 + **同一套外部硬判据评分**（`_score_runtime_scenario`，与外部 CLI 对比公平）。`agent_runner` 抛错 → `transport=hive_live_unavailable` + `benchmark_complete=False`（**fail-closed**）。
  - `behavior_eval_passed(report)`——门控核心：仅当**完整 + 可信 live transport（`hive_live`/`live_cli`）+ 无 fallback + 全场景 ready** 才 True；`repo_evidence`/partial/unavailable 一律 False。
  - `build_invoke_agent_runner(...)`——**真接 `invoke_agent`**（G0），`invoke` 可注入测试。默认调生产 `invoke_agent`；`tool_executor`（绑 workspace）+ agent_id/user_id 由 eval harness / E8 CI 提供，让 agent 文件工具写进 grader 检查的工作区。
  - `record_behavior_eval_run(...)`——G1 桥：behavior report → `record_eval_run`，**fallback transport → passed=False**（§9 诚实规则），reward 连续（场景均分/100）。
- `backend/app/services/evolution_verification.py`：新增 `agent_behavior_check` grader 分派 + `_run_agent_behavior_check`，把行为 eval 结果接进 `run_evolution_verification`（→ promotion 路径）。

**关键诚实边界**：`invoke_agent` 需 DB+LLM+workspace-bound `tool_executor`，**不能进 microVM**（§1.3）。本 E 交付**真接线 + fail-closed 门控**（可测）；真 LLM 端到端 live 执行在 **E8 CI**（提供 eval tenant/agent + tool_executor）。**不是桩**——`build_invoke_agent_runner` 默认调生产 `invoke_agent`，请求构造 + 产物提取被 `test_build_invoke_agent_runner_constructs_request` 钉住。

**TDD red→green**：`tests/evals/test_hive_live_runner.py` 13 用例，实现前 `ModuleNotFoundError`；green `13 passed`。共享模块回归 `test_evolution_verification + test_evolution_ledger + tests/evals/` = **62 passed**；ruff clean。

**验收映射**：行为结果写 eval_run（`test_record_behavior_eval_run_live_passed`）✓；fallback 不可 passed（`test_record_behavior_eval_run_fallback_not_passed` + `test_agent_behavior_check_grader_fallback_fails`）✓；grader 分派命中（`test_agent_behavior_check_grader_trusted_passes`）✓；agent 跑挂 fail-closed（`test_run_hive_behavior_eval_agent_error_is_fail_closed`）✓。

**gap 状态**：G0（Hive agent-core live runner）+ G1（行为结果入 ledger）机制已落，live CI 执行 = E8。

**仍待后续 E**：promotion 决策接 `execution_passed ∧ no_regressions` = **E3**；连续 reward 与 rubric 门控分离 invariant = **E4**；CI 真跑 = **E8**。

### E3 — promotion 硬门（execution_passed ∧ no_regressions）✅（2026-06-13）

**完成范围**（`backend/app/services/evolution_verification.py`）：
- `decide_verified_promotion` 加 `regression_report` 参数（向后兼容，默认 None）：verification 通过后，若 regression_report 表示退化（`passed=False`）→ **hold**（`behavior regression vs baseline`）；并加 `execution_passed` 显式断言（verification 含 `agent_behavior_check` 时必须通过，否则 reject）。
- 新增 `execution_evidence(verification_report)`：把 verification 里 `agent_behavior_check` 的存在性 + 结果显式化。
- 新增 `decide_behavior_gated_promotion(...)`——**canonical 硬门**：组合 E1（`compare_to_baseline` 的 regression_report）+ E2（`behavior_eval_passed`）+ static verification，三者全过才 promote。这是 E8 CI eval 路径调用的决策函数，自进化候选不过真实行为就晋升不了。

**生产接线**：`decide_verified_promotion` 的两个生产调用方（`skill_distiller.py:1145/1295`）签名向后兼容（新参数默认 None，静态 skill_guard 路径行为不变）；`decide_behavior_gated_promotion` 是 E8 CI 行为门的消费点。

**TDD red→green**：`tests/services/test_promotion_hard_gate.py` 13 用例，实现前 Pyright 缺符号/参数；green。回归 `test_promotion_hard_gate + test_evolution_verification + test_skill_distiller + test_skill_flywheel` = **45 passed**；现有 reject reason `"verification failed"`（skill_distiller exact-match）保持不变；ruff clean。

**验收映射**：退化候选 hold（`test_decide_verified_promotion_holds_on_regression` / `test_behavior_gated_holds_on_regression`）✓；行为 fail hold（`test_behavior_gated_holds_on_behavior_fail`）✓；verification fail reject（`test_behavior_gated_rejects_on_verification_fail`）✓；全过才 promote（`test_behavior_gated_promotes_when_all_pass`）✓。

**仍待后续 E**：CI 真跑 `decide_behavior_gated_promotion` 喂真 behavior_report + regression_report = **E8**。

---

## 附：关键文件锚点

| 文件 | 角色 |
|---|---|
| `backend/app/evals/bakeoff_runtime.py` | 外部 CLI live 行为 runner 骨架（已接 `run.py`，待 E2/E8 硬化与门控） |
| `backend/app/evals/self_evolution_bakeoff.py` | service-level 自进化检查（线 A，Hermes 注入/源码回退分待 E7 清理出门控路径） |
| `backend/app/evals/run.py` | 统一 eval runner（双层 CI 挂载点；待 E8 增加 fail-on-regression / require-live 退出语义） |
| `backend/app/services/evolution_ledger.py` | candidate→eval→promotion 审计链（E2/E3） |
| `backend/app/services/evolution_verification.py` | grader 分派（新增 `agent_behavior_check`，E2/E5） |
| `backend/app/services/code_execution/` | microVM 产物执行（E6） |
| `.github/workflows/harness-ci.yml` | CI（E8 双层改造，line 57-60 现挂 service-level self-evolution bakeoff） |
| `backend/app/evals/baselines/` | 版本化基线快照（新建，E1） |
