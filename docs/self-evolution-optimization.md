# Hive 自我进化系统：痛点分析与优化方向（v4）

> **背景**：扫描 `/Users/rocky243/Context Engineering/GenericAgent`（一个 ~3K 行的自演化 agent 框架）后，反向审视 Hive 当前的 4 层 MD 金字塔（T0→T2→T3→soul）演化系统。
> **目的**：定位优化空间，给出独立可推进的优化方向。
> **范围**：以自我进化为主（memory 演化、soul 演化、反思机制）。为了保护演化 substrate，包含必要的 kernel/runtime guardrail（trace ledger / loop detector），但不展开 tool / 渠道 / 企业权限。
> **v4 修订说明**：v3 的方向是对的，但实施形态仍偏“列优化点”。v4 收敛成一条更小的主线：**Evidence Envelope → Memory Promotion Ledger → Loop Guard → Reflection → Dream Protocol → INDEX Shadow/Switch**。核心原则是先保护演化 substrate，再提升自治能力。

## 2026-05-02 落地状态

本轮已把 v4 主线落到代码和测试，不再只是方案文档：

| Phase | 状态 | 落地路径 |
|---|---:|---|
| Phase 0 · Shadow Audit | ✅ 已完成 | `backend/app/services/self_evolution_audit.py`，输出 `tmp/reports/self-evolution-audit/*.json` |
| Phase 1 · Evidence Envelope | ✅ 已完成 | `backend/app/memory/t2_store.py`，`backend/app/services/extract_agent.py` |
| Phase 2 · Memory Promotion Ledger | ✅ 已完成 | `backend/app/services/evolution_ledger.py`，`backend/app/services/auto_dream.py` |
| Phase 3 · Loop Guard | ✅ 已完成 | `backend/app/kernel/loop_guard.py`，`backend/app/kernel/engine.py` |
| Phase 4 · Reportable Reflection | ✅ 已完成 | `backend/app/services/reflection_service.py`，`backend/app/runtime/hooks_setup.py` |
| Phase 5 · Dream Protocol Wiring | ✅ 已完成 | `backend/app/templates/DREAM_CONSOLIDATOR.md`，`backend/app/services/auto_dream.py` |
| Phase 6 · INDEX Shadow/Switch | ✅ 已完成 | `backend/app/memory/retriever.py` 新增 shadow 对比和默认关闭的 opt-in switch；production 默认暂不切换 |

验证命令：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate

pytest tests/memory/test_t2_store.py \
  tests/services/test_evolution_ledger.py \
  tests/services/test_self_evolution_audit.py \
  tests/kernel/test_loop_guard.py \
  tests/services/test_reflection_service.py \
  tests/memory/test_retriever_index_shadow.py \
  tests/services/test_auto_dream.py::test_dream_consolidator_template_is_loaded_into_prompt -q

pytest tests/memory \
  tests/services/test_evolution_ledger.py \
  tests/services/test_self_evolution_audit.py \
  tests/services/test_reflection_service.py \
  tests/services/test_auto_dream.py \
  tests/kernel/test_loop_guard.py \
  tests/kernel/test_engine.py -q

ruff check app/memory/t2_store.py \
  app/services/evolution_ledger.py \
  app/services/self_evolution_audit.py \
  app/kernel/loop_guard.py \
  app/services/reflection_service.py \
  app/memory/retriever.py \
  app/services/auto_dream.py \
  app/services/extract_agent.py \
  app/runtime/hooks_setup.py \
  tests/memory/test_t2_store.py \
  tests/services/test_evolution_ledger.py \
  tests/services/test_self_evolution_audit.py \
  tests/kernel/test_loop_guard.py \
  tests/services/test_reflection_service.py \
  tests/memory/test_retriever_index_shadow.py \
  tests/services/test_auto_dream.py

# 本轮最终全量验证
pytest -q
ruff check app tests
```

本轮刻意没有做的事：
- 没有把 P1/P2 `INDEX-first` 直接切为默认 production path；现在已有 shadow 对比和 opt-in switch，默认不切换以避免 recall regression。
- 没有开放 workspace override 修改 dream consolidator；dream 直接影响 soul，第一版只消费平台模板。
- 没有把完整 runtime trace 铺成大日志系统；本轮先用 `source_refs`、promotion ledger、reflection artifact 和 loop event 形成最小可回放闭环。

---

## 核心判断（一句话）

Hive 的问题**不是"没做"**，是**"关键链路还没有闭成 promotion pipeline"**——
T2 source metadata 有但缺 evidence 维度、INDEX 有但没有成为 P1/P2 检索导航层、heartbeat 已真实 SOP 化且支持 workspace override、`DREAM.md` 已存在但核心 LLM consolidator 仍由 Python prompt 字符串驱动、dream anti-pattern 写得细但缺可校准的正向评分。

最优方案不是四个方向并行铺开，而是把所有改动压到同一个闭环里：

```
observe → evidence-tag → candidate → validate → promote → audit/rollback
```

这条链打通后，A/D/C/B 都只是它的组成件。否则很容易出现“metadata 加了、trace 也写了、reflection 也有了，但 dream 仍然凭压缩文本直接升 soul”的半闭环。

---

## 引子：GenericAgent 给我们的最大启发

GenericAgent 核心代码才 ~2500 行，但它把**记忆系统的不变量直接压成"核心公理"**——

```
1. No Execution, No Memory（无行动，不记忆）
   写入 L1/L2/L3 的信息必须源自成功的工具调用结果。
   猜测、推理、未执行计划——一律不许写。

2. Sanctity of Verified Data（已验证数据神圣不可改）
   重构/GC 时严禁丢弃已验证信息。
   "记忆修改时极度小心，能不改就不改，宁愿不改也不要 overwrite。"

3. No Volatile State（禁存易变状态）
   时间戳、PID、Session ID、临时绝对路径——一律不许进 memory。

4. Minimum Sufficient Pointer（最小充分指针）
   上层只留能定位下层的最短标识，多一词即冗余。
```

这 4 条不是建议，是公理——extract、dream、heartbeat 全部必须遵守。Hive 在 `extract_agent.py:100-109`（`<tool_results_are_evidence>`）和散落的 prompt 里**有类似精神**，但**没有压成全系统统一的硬公理**，导致 T2/T3/soul 各处独立写规则、互相不一致。

---

## 关键边界：Hive ≠ GenericAgent

GenericAgent 让 agent 自己 patch `memory_management_sop.md` 是因为它**单 agent + 用户家目录 + 无多租户**。

**Hive 不能学这一步**：

| | GenericAgent | Hive |
|---|---|---|
| 部署形态 | 单 agent，用户独占 | 多租户多 agent 平台 |
| SOP 写权限 | agent 自由 patch | **平台/用户写，agent 只读** |
| evolution 协议改动 | agent 决定 | 走平台版本控制 |

Hive 的优化应该是"把启发式从代码字符串移到 agent **可读、可引用、可对照**的 SOP"——不是"让 agent 改自己的 evolution 逻辑"。这一刀比 GenericAgent 紧，但符合 Hive 的多租户基因。

---

## 校准后的 7 个痛点

按尖锐程度重排，每条都标注 Hive 已做了什么 / 还差什么。

---

### 痛点 1：T2 有 source provenance，缺 evidence 维度

**Hive 现状（已做）**：
- `t2_store.py:68` `_SOURCE_BUCKET_WEIGHTS` 已按 `human / autonomous / system` 分桶给权重
- `t2_store.py:149` `format_t2_entry` 写入 `[w=][src=][cat=]` metadata
- `extract_agent.py:100-109` 有 `<tool_results_are_evidence>` 段，把工具调用结果列为一等证据

**还差什么**：source 是"消息从哪来的"，但**evidence 是"这条信息可信度类型"**——一个 `human` source 既可能是用户陈述，也可能是 agent 推理后用户默许。当前 schema 区分不出来。

**修正方向**：T2 entry 增加 evidence 维度：
```
[evidence=tool_verified | user_stated | inferred | system_observed]
```
dream 升 T3/soul 时硬约束：`inferred` 类条目必须经过 N 次确认或被另一类证据交叉验证才能升。

---

### 痛点 2：INDEX 已存在，但还不是 P1/P2 检索导航层

**Hive 现状（已做）**：
- `md_store.py:232` `rebuild_index` 在维护 `# Memory Index` 表（File / Category / Items / Updated / Load）
- `auto_dream.py:833-837` dream 后会重建 `memory/INDEX.md`

**还差什么**：`retriever.py:267` `_retrieve_t3_direct` 仍直接读 T3 entries 做 per-entry 评分：P0 的 `feedback.md / blocked.md` 全保，P1/P2 再按 query 评分。这个策略比“全文件塞 prompt”已经好很多，但 `INDEX.md` 仍是 shadow artifact，不是检索路径上的 L1 navigation gate。

**修正方向**：把 INDEX 升级为 L1 navigation，但不要一刀切成“只注入 INDEX”：
- P0：`feedback.md / blocked.md` 继续直接注入，用户纠正和失败模式不应被索引层误筛掉
- P1/P2：默认注入 `INDEX.md` 的紧凑摘要（≤30 行），再由 retriever / agent 按需展开相关 section
- 新增 `read_memory_section(file, section)` 或复用受控 `read_file` wrapper，让 agent 能显式按索引展开

GenericAgent 的 L1（`global_mem_insight.txt`）和 Hive 的 INDEX 形态相似，但 GenericAgent 更激进：L1 是主要入口。Hive 不应照搬为唯一入口，因为 P0 memory 在当前架构里承担安全/纠偏职责；更合理的是 **P0 直达 + P1/P2 INDEX-first**。

---

### 痛点 3：DREAM.md 已存在，但还没成为 dream 核心决策入口

**Hive 现状（已做）**：
- `heartbeat.py:413-427` `_load_heartbeat_instruction` 已经优先读 workspace 里的 `HEARTBEAT.md`，fallback 到 `templates/HEARTBEAT.md`
- 这意味着：用户/平台 patch 一份 `HEARTBEAT.md` 就能改变 curation 行为，**不需要发版**
- `templates/DREAM.md` 已存在，并且 `auto_dream.py:680-681` 定义了 `_DREAM_TEMPLATE_PATH`
- `templates/DREAM.md:8-13` 明确说它处理 procedural file-maintenance side，而 structured decision work 由 separate LLM consolidator 负责

**还差什么**：`_DREAM_TEMPLATE_PATH` 当前只作为模板存在；核心 `_dream_llm_consolidate()` 仍在 `auto_dream.py:402-416` 调 `_build_dream_consolidation_user_prompt()`，而后者使用 `auto_dream.py:117` 的 `_DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE`。也就是说：**DREAM.md 有了，但核心 promote / contradiction / preservation decision contract 还没从 Python 字符串里解耦出来**。

**修正方向**：把 dream 拉到 heartbeat 同样的可读协议水平——
- 将 `_DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE` 的 promote 启发式 / anti-pattern / soul section schema 合并进 `templates/DREAM.md`，或拆成 `templates/DREAM_CONSOLIDATOR.md`
- 新增 `_load_dream_instruction(agent_id)`，明确是否允许 workspace override；如果允许，仍必须是**用户/平台写，agent 只读**
- Python 只保留：调度、flock、原子写、JSON schema 校验、不可绕过的不变量

这不是“新增 DREAM.md”，因为文件已经存在；真正缺口是**让 runtime dream path 消费这份协议**。

---

### 痛点 4：dream anti-pattern 已细，缺正向评分

**Hive 现状（已做）**：
- `auto_dream.py:229-241` 的 `<anti_patterns>` 写得相当具体——单次出现不升、active objective 不升、wake policy 不升、focus row 不升、未稳定的反转不升……

**还差什么**：黑名单是"什么不能升"，但缺**正向评分**——"什么该升 + 多该升"。

**修正方向**：T2 entry 进一步加两个评分字段，但先记录后收紧，不要第一版就硬阈值：
```
[novelty=0.0-1.0]      # LLM zero-shot 不知道的程度
[reusability=0.0-1.0]  # 未来 N 个 session 会用到的预期
```
第一阶段只记录分数和 dream 决策结果，跑 1-2 个周期看分布；第二阶段再加门槛，例如：`novelty × reusability` 达到校准阈值，且 `evidence ≠ inferred`，才允许 T3 → soul。原因是 LLM 自评分会漂，没观测分布就硬切容易误杀真实高价值记忆。

GenericAgent 的判定公式："AI 训练数据无法覆盖" × "对未来协作有持久收益"。这两个相乘，单方面强不够。

---

### 痛点 5：没有任务收尾仪式

**Hive 现状（已做）**：
- T0 已有 `behavior/chat-*.md`、`trigger-*.md`、`delegation-*.md`（`hooks_setup.py:120` 等）
- SESSION_CLOSE drain extractor、写 T0
- heartbeat / dream 各有 system T0 自我审计

**还差什么**：上述都是**被动记录**——agent 不会在任务结束时主动**写实验报告 / 失败复盘 / 下一步假设**。GenericAgent 的 `autonomous_operation_sop` 收尾 4 件事是 agent 自己执行的强制仪式，Hive 没有对标。

**修正方向**：SESSION_CLOSE 加 reportable 任务判定：
- 触发条件：超过 N 轮 / 有 commit / 用户明确点赞 / 失败明显
- 触发后：agent 必须执行收尾仪式（写 `temp/reports/RXX_<topic>.md`，**包括失败也写**）
- 报告字段：意图 / 假设 / 行动 / 结果 / 根因 / 下次改

---

### 痛点 6：失败没专门反思通道

**Hive 现状（已做）**：
- T2 的 `errors.md`、T3 的 `blocked.md`、`evolution/blocklist.md` 都存在
- 但偏**事实型 / 计数 / summary**

**还差什么**：缺**结构化反思**——"决策 → 证据 → 结果 → 根因 → 下次 policy"。事实型只能告诉你"X 失败过 N 次"，反思型才能告诉你"我为什么尝试 X，下次遇到这个 pattern 应该改用什么"。

**修正方向**：不要把完整反思直接塞进普通 T2。更稳的是新增结构化 reflection ledger，然后把可复用结论投影到 T2/T3：
- 原始反思：`memory/reflections/failure_reflections.jsonl` 或 `evolution/reflections.md`
- T2 投影：只写 distilled `blocked_pattern` / `strategy`，并带 `trace_ref`

强结构 schema：
```yaml
- decision: 我尝试了 X
  evidence: 当时看到 Y 所以选 X
  outcome: 失败/部分成功/意外结果
  root_cause: 根因
  next_policy: 下次遇到 Y 应该选 Z
  trace_ref: logs/YYYY-MM-DD/traces/<id>.jsonl
```
dream 时 reflection 类条目可以有更高 review priority，但只有 `next_policy` 被后续 trace 验证后，才应 promote 到 T3/soul。

---

### 痛点 7（新增）：runtime trace 不够可回放 + 无 semantic loop detection

这是 v1 漏掉的，**和 A 同 P0 优先级**。

**Hive 现状（已做）**：
- T0 behavior MD 保留消息和 tool_calls
- system T0（heartbeat-*.md、dream-*.md）记录决策推理
- `evolution_ledger.py:1-5` 已经要求自动 prompt/skill/policy 变更留下 candidate / eval / promotion decision
- `evolution_ledger.py:77-103` 的 eval run 可记录 `traces`

**还差什么**：

**A. 可回放 trace**：已有 `evolution_ledger.jsonl` 更像“候选变更 → eval → promotion”的审计链，不是 heartbeat / trigger / task 的 per-invocation runtime trace。当前 T0 是消息和 tool call 的记录，但缺每步输入摘要 / 工具序列 / 结果 digest / 状态变化 / 退出原因的结构化链路。如果 dream 把"用户偏好 X"升进 soul 但其实是 agent 幻觉——你只能从 T2/T3 反推，缺少可直接引用的 ground-truth trace。

**B. Loop detection**：第一份对比文档已经指出 Mercury 的 6 层 loop detection 是亮点（25 总 / 12 失败 / 同工具同参 3 次 / 同工具全失败 4 次 / 5 次无 tool call / 文本重复 3 次）。Hive 当前主要依赖 `kernel/engine.py:1273-1285` 的 `max_rounds` 循环上限和 80%/96% warning；`AgentRuntimeConfig` 默认是 200，heartbeat 显式传 15。它缺的是 semantic loop detector，而不是“固定 50 轮”。

**修正方向**：

**D1. Runtime Trace Ledger**：
- 给 heartbeat / trigger / 重要 task 加结构化 trace（JSONL 或 MD with frontmatter）
- 字段：`step / input / tool / args / result_digest / state_delta / exit_reason`
- 可回放、可 diff、可作为 dream 升级判断时的"原始证据"
- 不要混淆现有 `evolution_ledger.jsonl`：它继续管 candidate/eval/promotion；runtime trace ledger 管 invocation-level execution evidence

**D2. Loop Detector**：
- 移植 Mercury 6 层检测到 `kernel/engine.py`
- 检测到 loop 立即中止，写 runtime trace event，并向 T2/T3 投影一条 distilled `[blocked_pattern]`（只带摘要和 `trace_ref`，不要把完整 trace 塞进 T2）
- 防止无效工具循环污染 T0/T2/T3 演化 substrate

---

## 更优实施方案：一条主线，而不是四个并行方向

v3 的 A/D/C/B 方向都对，但最佳实施顺序要变。原因很简单：自我进化最怕的不是“少学”，而是“学错后被系统性放大”。所以第一优先级不是让 agent 更主动，而是让每一次升级都有证据、候选、验证、回滚。

### Phase 0 · Shadow Audit：先量化污染面，不改行为

**目标**：先知道当前 memory substrate 有多脏、dream promote 有多少缺证据、loop 有多常见。

**动作**：
1. 新增只读审计脚本/服务函数，扫描 T2/T3/soul：
   - `t2_entries_without_evidence`
   - `t3_entries_without_source_ref`
   - `soul_lines_without_promotion_record`
   - `dream_promotions_without_trace_ref`
   - `retriever_index_shadow_miss_rate`
2. 读取最近 T0 / heartbeat / dream system logs，统计：
   - 同工具同参重复次数
   - 失败工具重复次数
   - 无工具空转轮数
   - 文本重复输出
3. 输出一份 `tmp/reports/self-evolution-audit/<timestamp>.json`。

**为什么先做**：没有 baseline，就无法判断后面的 evidence / trace / INDEX-first 是真优化还是只是更复杂。

---

### Phase 1 · Evidence Envelope：所有写入先带证据壳

**目标**：先收紧写入边界，而不是先重构 dream / retriever。

T2 entry schema 建议保持 markdown 兼容，但增加最小证据壳：

```text
[w=0.85][src=human][cat=feedback][ev=user_stated][conf=0.90][vol=stable][refs=t0:chat/2026-05-01.md#L42-L48][nov=0.70][reuse=0.80]
```

字段解释：
- `ev`：`tool_verified | user_stated | inferred | system_observed`
- `conf`：提取置信度，不等于 promote 资格
- `vol`：`ephemeral | session | project | stable`
- `refs`：最小证据指针，优先指向 T0 / runtime trace / tool digest，不塞原文
- `nov/reuse`：先记录，暂不硬 gate

关键点：**source_ref 比 full raw trace 更重要**。不要把每个 tool step 都塞进 T2；T2 只需要能回到原始证据。

---

### Phase 2 · Memory Promotion Ledger：复用 evolution_ledger 的候选链

**目标**：把 T2→T3、T3→soul 都变成 candidate/eval/promotion，而不是 dream 直接写。

当前 `evolution_ledger.py` 已经有 candidate / eval_run / promotion_decision 结构。更优做法不是另造一套 memory ledger，而是扩展它：

```json
{
  "schema": "memory_promotion_candidate.v1",
  "event": "candidate",
  "target_type": "memory:t3|memory:soul",
  "target_id": "feedback.md|soul.md#Learned Behaviors",
  "source_refs": ["t2:learnings/feedback.md:12", "t0:behavior/chat-2026-05-01.md#L42-L48"],
  "evidence": "user_stated",
  "novelty": 0.72,
  "reusability": 0.81,
  "volatility": "stable",
  "proposed_diff": "..."
}
```

Promotion rule 第一版不要复杂：
- `inferred` 不能升 soul
- `ephemeral/session` 不能升 T3/soul
- `soul` promotion 必须有 `source_refs >= 2` 或 `user_stated/tool_verified`
- 每个 promotion 必须有 rollback ref

这一步是最关键的一刀：**dream 不再是“写 memory 的智能体”，而是“提出 memory promotion candidate 的智能体”。**

---

### Phase 3 · Loop Guard：在污染进入 memory 前截断

**目标**：loop detection 不只是省 token，它是 memory hygiene guardrail。

实现位置应在 `kernel/engine.py` 的 tool loop 内部，独立于 dream/heartbeat：
- total tool calls 上限
- failed tool calls 上限
- 同工具同参重复
- 同工具连续失败
- 无工具空转
- assistant 文本重复

命中后：
1. 中止 invocation，返回明确 `loop_guard_triggered`
2. 写 runtime trace event
3. 只投影一条 distilled `blocked_pattern`，带 `trace_ref`
4. 禁止把完整失败循环直接进入 T2/T3 substrate

这里不要先做复杂 AI 判断；第一版用 deterministic counters，测试可控。

---

### Phase 4 · Reportable Reflection：异步收尾，不阻塞会话

**目标**：补 GenericAgent 的“任务收尾仪式”，但用 Hive 的平台形态实现。

不要在 `SESSION_CLOSE` 同步逼 agent 继续写长报告。更稳做法：
- `SESSION_CLOSE` 只做 reportable 判定并 enqueue
- 后台 reflection worker 生成 report artifact
- report 原文进入 `memory/reflections/*.jsonl` 或 `evolution/reflections.md`
- T2 只接收 distilled policy projection

Reportable 条件建议：
- tool rounds 超过阈值
- 有 commit / 文件写入 / 部署 / 外部 action
- loop guard 命中
- 用户明确纠正
- task 失败或部分失败

这样既有反思，又不会把每次普通聊天都变成 memory churn。

---

### Phase 5 · Dream Protocol Wiring：DREAM.md 接入核心 consolidator

**目标**：不是新增 `DREAM.md`，而是让核心 `_dream_llm_consolidate()` 真的消费协议文件。

建议拆成两层：
- `templates/DREAM.md`：procedural maintenance protocol（当前已有）
- `templates/DREAM_CONSOLIDATOR.md`：promotion decision contract（从 Python string 外置）

Python 保留不可商量的不变量：
- JSON schema 校验
- source_refs 必填
- evidence gate
- rollback ref
- atomic write / flock
- max promotions per run

workspace override 要谨慎：heartbeat 可以 workspace override；dream consolidator 直接影响 soul，第一版建议只允许平台模板，等 promotion ledger 稳定后再开放用户级 override。

---

### Phase 6 · INDEX-first 先 shadow，再切 production

**目标**：retriever 是 prompt recall 路径，不能直接换。

更稳方案：
1. 保持当前 `_retrieve_t3_direct()` 作为 production path
2. 并行跑 `retrieve_t3_index_shadow()`，不注入 prompt，只记录对比：
   - P0 是否全保
   - P1/P2 top-k 是否覆盖旧路径高分 entry
   - token 节省
   - miss 的 entry 是否后来被用户/任务需要
3. shadow 数据稳定后，再切 P1/P2 INDEX-first

这是 v4 和 v3 最大差别之一：**retriever 改动必须 shadow-first**。否则你可能为了 token 效率牺牲 recall，短期看更干净，长期会让 agent 忘掉关键上下文。

---

## 优先级矩阵（v4）

| 优先级 | Phase | 解决 | 行为风险 | 战略意义 |
|---|---|---|---|---|
| P0 | Shadow Audit | 先量化污染/缺证据/loop | 无 | 给后续验收基线 |
| P0 | Evidence Envelope | T2/T3/soul 证据壳 | 低 | 数据地基 |
| P0 | Memory Promotion Ledger | dream 不再直接升层 | 中 | 学习地基 |
| P0 | Loop Guard | 防止循环污染 substrate | 中 | runtime 地基 |
| P1 | Reportable Reflection | 主动复盘但不乱写 memory | 中 | 主动演化 |
| P1 | Dream Protocol Wiring | 把核心决策协议外置 | 中 | 协议可审计 |
| P2 | INDEX Shadow/Switch | token/recall 优化 | 中高 | 检索效率 |

**结论**：最优不是“先做 A 或 B”，而是先做 **Phase 0-3**。这四步完成后，Hive 才有可靠的自我进化底座；Phase 4-6 才是在底座上增强自治能力。

---

## 战略洞察

Hive 的自我进化系统**地基扎实**：
- 4 层金字塔（T0/T2/T3/soul）✅
- 双周期任务（heartbeat 45min + dream 4h+3sessions）✅
- KAIROS 持久 session ✅
- system T0 自我审计 ✅
- T2 source provenance + INDEX + heartbeat SOP + DREAM.md template ✅（但都还不是完整闭环）

**地基之上的协议层缺最后一公里**：
- 公理（A 缺）
- evidence 维度（A 缺）
- source_refs / rollback refs（promotion pipeline 缺）
- memory promotion ledger（dream 直写改为 candidate/promotion 缺）
- 正向评分与校准阈值（先观测后启用）
- P1/P2 INDEX shadow/switch（不是直接切）
- dream protocol runtime wiring（核心 consolidator 仍未消费协议文件）
- 仪式化反思（C 缺）
- runtime 可回放 trace + semantic loop detection（D 缺）

补完后的 Hive 不是"造一套 GenericAgent"——是把 Hive 已经走到 70% 的演化系统**收紧成可审计、可回放、可回滚的 promotion pipeline**。

---

## 公理草案（Phase 1/2 落地用）

> 落地到 `workspace/protocols/memory_axioms.md`，用户/平台维护，agent 只读。

```markdown
# Hive Memory Axioms（Hive 记忆系统核心公理）

## 公理 1：Evidence-Tagged Writes（证据标记不可缺）
凡写入 T2/T3/soul 的 entry 必须有 evidence 标记之一：
- [evidence=tool_verified]：来自工具调用成功结果
- [evidence=user_stated]：来自用户明确陈述
- [evidence=inferred]：agent 推理（仅允许进 T2，禁止直接进 T3/soul）
- [evidence=system_observed]：来自 trace ledger / system T0 的可观测事实
- [source_refs=...]：最小证据指针必须能回到 T0 / runtime trace / tool digest / user message

## 公理 2：Sanctity of Verified Data（已验证数据神圣）
- T3/soul 修改：能 patch 不 overwrite，能不改就不改
- dream 改 soul 必须先 read 当前内容，输出 diff 而非全量
- 删除已验证信息需 confidence > 0.85，且记录删除原因到 system T0
- inferred 升级为更强 evidence 必须有交叉验证

## 公理 3：No Volatile State（禁存易变状态）
禁止写入 T2/T3/soul：
- 时间戳、日期（除非是规律性时间，如"用户每周一汇报"）
- Session ID、PID、临时 token
- 临时绝对路径（除非是用户环境的稳定路径）
- 当前任务进度、in-flight 状态、active objective ids

## 公理 4：Minimum Sufficient Pointer（最小充分指针）
- 索引层只写关键词 + 文件名定位，不写 how-to
- T3 条目：自包含的最短形式，不重复 T2 已有内容
- soul：只放跨 session 不变的身份级事实
- INDEX 强制 ≤30 行；P0 memory 可直达，P1/P2 memory 默认 INDEX-first

## 公理 5：Evolution Trace Required（演化必须有可回放证据）
- heartbeat / dream 升级 T3 → soul 必须能引用具体 trace
- 升级动作本身写 system T0（含 before/after diff）
- 反向回退路径必须存在（任何升级都可在 N 个周期内被反例推翻）
- dream 只能提出 promotion candidate；真正写 T3/soul 必须经过 ledger-backed apply
```

---

## 验收标准（不是只看代码合并）

每个阶段必须能被测试或报告验证：

| 阶段 | 验收信号 |
|---|---|
| Shadow Audit | 生成 `tmp/reports/self-evolution-audit/*.json`，包含 T2/T3/soul 证据缺口统计 |
| Evidence Envelope | 老 T2 entry 可 parse；新 T2 entry 写入 `ev/conf/vol/refs/nov/reuse`；dedup 不丢 metadata |
| Memory Promotion Ledger | 任一 T3/soul promotion 都能找到 candidate/eval/promotion/rollback record |
| Loop Guard | 测试覆盖同工具同参、连续失败、文本重复、无工具空转；命中后不进入普通 extraction substrate |
| Reportable Reflection | report artifact 和 T2 distilled projection 分离；普通聊天不会触发 report churn |
| Dream Protocol Wiring | `_dream_llm_consolidate()` 的 prompt 来源可追溯到 template；Python 只保留 schema/invariant |
| INDEX Shadow/Switch | shadow report 证明 P0 全保、P1/P2 miss rate 可接受，再切 production |

---

## PR 拆解（v4）

| PR | 内容 | 依赖 | 估时（AI 协作）|
|---|---|---|---|
| PR-0 | Shadow audit report（只读，无行为变更） | 无 | 1d |
| PR-1 | Evidence Envelope：T2 schema + parser backcompat + extract prompt | PR-0 | 1.5d |
| PR-2 | Memory Promotion Ledger：扩展 evolution_ledger 支持 memory promotion | PR-1 | 2d |
| PR-3 | Loop Guard：kernel deterministic loop detector + tests | PR-1 | 1.5d |
| PR-4 | Reportable Reflection：异步 report artifact + distilled T2 projection | PR-2, PR-3 | 1.5d |
| PR-5 | Dream Protocol Wiring：DREAM_CONSOLIDATOR template + runtime loading | PR-2 | 2d |
| PR-6 | INDEX Shadow：并行检索对比报告，不切 production | PR-1 | 1.5d |
| PR-7 | INDEX Switch：P0 direct + P1/P2 INDEX-first production gate | PR-6 | 1d |

核心闭环（PR-0 到 PR-3）≈ 6 个 AI 协作日；完整增强 ≈ 12 个 AI 协作日。比 v3 少一点，不是因为事情更少，而是因为不再把 Trace Ledger / Reflection / Retriever 重构互相耦在一起。

---

## 不该做的事（明确边界）

避免方向漂移：

❌ **不要让 agent 自由 patch evolution protocol**——SOP 写权限在用户/平台，不在 agent。这是 Hive 多租户基因，不是 GenericAgent 的单 agent 自治。

❌ **不要造新一套 4 层 memory**——Hive 的 T0/T2/T3/soul 已经成立，加 evidence/novelty/reusability 是收紧，不是替换。

❌ **不要把 retriever 改成 embedding 全套**——和 INDEX-first 方向相反。这里要补的是 P1/P2 的低成本导航层；embedding 是另一层更复杂的优化，不是 v4 焦点。

❌ **不要在 P0/P1 阶段动多 agent 学习池**——跨 agent share 涉及隐私边界，应该是 v4 话题。先把单 agent 演化收紧。

❌ **不要先写 full runtime trace 再想怎么用**——先做 `source_refs` 和 promotion ledger，runtime trace 只补能支撑回放/验证的最小字段。

❌ **不要让 dream 直接写 soul**——dream 可以提出 candidate，真正 apply 必须有 promotion record 和 rollback ref。

❌ **不要直接切 INDEX-first production**——必须先 shadow 对比，否则 recall regression 很难定位。
