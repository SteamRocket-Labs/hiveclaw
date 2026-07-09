# 动态记忆激活（Dynamic Memory Activation）设计 — 2026-07-09 v0.1 讨论稿

> 回答 owner 的问题：QKV 收缩之后，"让记忆有随上下文变化的动态权重"这件事到底该怎么做。
> 基线证据：G-Brain 一手源码解剖 + 9 框架动态权重矩阵 + ACT-R/HippoRAG 应用现状（完整调研存 memory-research scratchpad `dynamic-memory-findings.md`）+ Hive 资产盘点（本文 §3）。
> 纪律：先文档拍板，后一次改完。本稿为 v0.1 讨论稿，待 owner 拍板后转正。

## 1. 问题定义：平行召回缺的是什么

"平行召回"的精确诊断不是"没有权重"——Hive 的 `ActivationScorer`（memory/activation.py）已有 goal/owner/company/open_loop/retention/confidence/usage_heat 七维加权，`personalized_pagerank` 已接线两处（wiki_retrieval.py:114、personal_knowledge_service.py:2734）。真正缺的是**三种动态性**：

1. **会话内动态**：打分对 session 无记忆。本轮刚讨论的主题、刚激活的记忆、刚触碰的文件，不影响下一次召回。每 turn 都是无状态的从零打分——这是"权重不随上下文变化"的本质。
2. **使用反馈动态**：检索命中已 `bump_access`（retriever.py:191），但 usage_heat 被刻意压到 0.05 只做 tie-break，且 QKV-I 建好的 heat_delta / FeedbackCredit 曾经只写不读——"越被成功使用、越容易再被召回"的强化环需要闭合。
3. **任务目标动态**：更糟——goal 语义在生产路径**根本没进打分**。ActivationScorer 有 `goal_terms` 字段（activation.py:17）和 `goal_relevance` reason（:60-62），但 `memory_service.py:208` 构造 ActivationContext 时**从不填充 goal_terms**（只填 query/principal_stack/owner/company）。于是 `_overlap(query_terms | set(goal_terms), content)` 里 goal_terms 恒为空集——**当前的 `goal_relevance` 实为 query overlap 的误命名，不是 active goal modulation**（Codex 2026-07-09 核实）。TaskModulation 要做的不是"改进"目标打分，而是**首次真正把 goal 语义接进召回**。

> **设计律（Codex 提炼，采纳为本设计第一句）：Attention is for recall/ranking, not for truth.** T0/T2/T3/Memory Gate 仍然独占真相；本层只决定"这一轮该优先唤起什么"，是 truth surface 之外一层**可重建、可解释、受预算约束**的 dynamic recall layer。任何让 attention 触碰或改写真相的设计都是越界。

**QKV 层的教训**（审计定论）：问题意识正确，但把"动态权重"翻译成了 harness 层的机械词重叠打分，且**另建平行系统**而非升级已有 scorer——两个错误叠加使它沦为非承载脚手架。本设计的第一原则：**升级 ActivationScorer 主路径，不建任何平行层**。

## 2. 调研结论（证据基座）

六维矩阵（写时权重/读时动态/时间衰减/使用强化/图传播/任务敏感）扫过 G-Brain、Mem0、Zep/Graphiti、Letta、MemOS、MemoryOS、A-MEM、cognee、LangMem、Memobase、MIRIX：**没有任何系统六维全有**，多数只点亮 1-2 维。分维最深者：

- **时间**：Zep/Graphiti 双时态（valid_at/invalid_at 软失效，回答"某时刻的真"）——语义正规但不是权重衰减。
- **热度**：MemoryOS 唯一闭式 heat 公式 `α·N_visit+β·L_interaction+γ·exp(−Δt/μ)`——但只驱动分层 promotion 不驱动检索排序（它的天花板）。
- **图扩散**：HippoRAG（PPR + 海马体模式补全隐喻，2WikiMultiHop R@5 89.1 vs 68.2）与 SYNAPSE（图激活传播 + ACT-R fan effect，LoCoMo F1 40.5 vs A-Mem 33.3）——**唯一有硬 benchmark 增益的"动态"**。
- **反馈**：cognee（feedback_influence 权重注入 ranking）。
- **工程堆叠**：G-Brain（半衰期 recency + take_count 使用计数 + hub adjacency + cross-encoder rerank，全部小倍率 1.05-1.6× bound）——"self-wiring" 实为零 LLM 的 wikilink 确定性抽取 + 夜间 enrichment cron。
- **事实标准**：Generative Agents 的 `recency × importance × relevance` 线性 blend（0.995^小时 指数衰减 + LLM importance 1-10 + cosine）被几乎所有后续系统采用。
- **ACT-R 现状**：无人完整落地（幂律衰减被指数替代、`ΣW_j·S_ji` 被 PageRank 替代、噪声/阈值被领域共识抛弃——概率召回损害可靠性）；且无 head-to-head 证明完整 ACT-R 打赢简单启发式。

**四条系统性空隙（= Hive 的差异化机会）**：①频率强化闭环（retrieval 反哺记忆强度——A-MEM 连 retrieval_count 都不自增，全行业几乎没做）②任务/目标条件化召回（CoALA 综述点名 understudied）③原理性激活模型 ④统一六维可解释 scorer。

## 3. Hive 已有资产（本设计的原料，全部审计确认）

| 资产 | 位置 | 现状 | 在本设计中的角色 |
|---|---|---|---|
| ActivationScorer 七维加权 | memory/activation.py:24-30 | 真承载主路径 | **统一激活方程的宿主**（升级不重建） |
| RRF 混合检索 | retriever + personal KB 四通道 | 真接线 | Relevance 候选生成层（保留） |
| personalized_pagerank | relation_graph.py:235 | 接线两处（HippoRAG-inspired 自述） | ContextBoost 的扩散引擎（换 seeds） |
| access_log（count+last_accessed） | access_log.py:21 + lifecycle sidecar | 检索命中即 bump | BaseLevel 频率强化的半个闭环 |
| heat_delta / FeedbackCredit 反馈信号 | session_feedback.py | sidecar credit 已接 lifecycle_store；ranking 权重仍需按本设计收敛 | FeedbackCredit 的输入 |
| T2 activation_keys | t2/prompts.py:109（LLM 撰写） | 留在 T2 文件（SQLite 投影按拍板删除） | 记忆侧语义键（K 的正确形态） |
| relation graph + wiki links | relation_graph.py | 真接线 | 扩散的图基底 |
| context_budget | runtime/context_budget.py | T1 真机制 | 注入预算控制（不动） |
| score_trace | KB search 已有 | 真接线 | 可解释性习惯（延续到统一 scorer） |
| Goal Mode（对齐中） | §7.2-A 九件 | 半装配→全对齐 | TaskModulation 的目标语义来源 |

结论：**Hive 已站在业界最前的图传播线上，且拥有全行业几乎没人接通的两块原料（频率强化半环 + 反馈信号 sidecar）**。缺的是把它们接成一个统一的、有会话态的激活方程。

## 4. 设计：统一激活方程（四个乘法项）

**四个乘法项**（FeedbackCredit 是 BaseLevel 的子项，不是独立第五项——见 4.3）：

```
Activation(m | t) = Relevance(m, Q_t)                 # 语义相关 —— 候选生成，已有 RRF，不动
                  × ContextBoost(m, W_t)              # 会话内激活扩散 —— 新建（核心增量）
                  × BaseLevel(m)                      # 基础激活：频率 + 幂律衰减 + 反馈 credit —— 升级
                  × TaskModulation(m, G_t)            # 目标调制 —— 升级
```

其中 Q_t = 当前 turn 的检索意图；W_t = 会话工作集（随 turn 演化）；G_t = active goal。所有 boost 因子遵守 **G-Brain 纪律：小倍率、有界（建议 [0.8, 1.6]）、乘法叠加、全程 score_trace 落 reasons**——防止任何单一信号劫持排序；Relevance 之外的组件计算**全确定性零 LLM**（唯一例外见 4.4 的低频事件）。

### 4.1 Relevance（不动）
RRF 混合检索继续做候选生成。它的职责定位从"决定最终排序"降为"生成候选池 + 提供语义相关基分"。

### 4.2 ContextBoost —— 会话内激活扩散（核心增量，"随上下文变化"的直接实现）

新建 **session working set `W_t`**：本会话已激活过的记忆、已讨论实体、已触碰文件/工具的集合，随 turn 增量演化（新激活项进入、旧项按 turn 距离衰减）。存放于 session 态（chat session metadata 或 session_state 目录，重启可恢复）。

**W_t 的 ACL/隐私边界（硬约束，Codex 2026-07-09 要求）**：W_t 是可持久化、可跨重启恢复的 session state，因此**只允许存指针与统计，禁止存正文与敏感载荷**。每项 schema = `{source_ref | id | entity | file_ref, strength: float, last_turn: int, ts}`——即"指向哪条记忆/哪个实体/哪个文件"的引用 + 激活强度 + 时间戳。**禁止**把 PL3/PL4 记忆正文、敏感 tool payload、KB 文档内容塞进 W_t。召回时按 source_ref 现取现用并**重新过 sensitivity/ACL 门**（现有 suppression 逻辑），W_t 本身不承载可读内容——它是激活的"地址簿"不是"内容缓存"。这样即使 session state 被导出/恢复/审计，泄漏面也只是"激活过哪些 id"，不含机密正文。

每次召回：以 W_t 中强度 top-N 项为 seeds，在 relation graph 上跑 **PPR**（复用 :235 实现），扩散分归一后作为 ContextBoost 因子。效果：聊到"Railway 部署"之后的若干 turn 内，与部署相关联的记忆（经图上一两跳）整体预热；话题切换后 W_t 演化，预热面随之迁移——**同一 query 在不同会话语境下召回不同记忆**，这就是所要的"动态权重随上下文变化"。

这才是该向 transformer 借的思想：**不是 QKV 矩阵乘法，而是 KV-cache 式的增量会话态**——有状态、增量更新、随序列演化。SYNAPSE/HippoRAG 已为图扩散提供 benchmark 背书；Hive 的增量是把 seeds 从"query 关键词"升级为"会话工作集"。

### 4.3 BaseLevel —— 频率强化 + 幂律衰减 + 反馈 credit（升级 _usage_heat，接通两个断环）

替换 `_usage_heat` 的线性截断计数，采用 ACT-R base-level 的工程化简版（只取无争议的核，噪声/阈值明确不要）：

```
BaseLevel(m) = bound( 1 + k · ln(1 + Σ_j t_j^(−d) + credit(m)) )
```

- `t_j` = 最近 K 次访问距今时长（lifecycle sidecar 从"count+last_accessed"扩展为"最近 K 次时间戳环形数组"，K≈8）；`d`≈0.5（ACT-R 经典幂律）。频率 + 近因合一，**"越被成功召回、越易再被召回"的行业空白闭环**由已有 bump_access 直接喂养。
- `credit(m)` = FeedbackCredit：把 owner feedback 接回——useful → 该 turn 激活过的记忆 credit+；misleading → credit−（cognee 已验证反馈注入 ranking 这条路）。credit 存 lifecycle sidecar 元数据，**不改 MD 正文**（Memory Gate 合规：机械记账不是语义写入）。
- 治理边界：BaseLevel 只做小倍率 boost，不能压过 sensitivity/lifecycle suppression（现有 suppressed 逻辑优先级不变）。

### 4.4 TaskModulation —— 目标条件化召回（升级 goal_weight，与 Goal Mode 对齐汇合）

两级：
- **确定性级（每 turn，零 LLM）**：active goal 的 objective embedding 作为第二 query 通道参与 Relevance（双查询融合），替代现在的布尔 +0.25。
- **智能级（低频事件驱动）**：goal 设立/更新、compaction、阶段切换时，由 LLM 产出一次 **attention set 声明**（与当前目标相关的主题/实体/文件清单）——写入 W_t 作为持久种子，直到 goal 终态或下次声明。这是 L1 合规的正确位置：智能判断交给模型、事件驱动不付每 turn 成本。落点恰好与 §7.2-A（Goal Mode 九件全对齐）汇合——A7 的 objective steering 与此共用一条链。

### 4.5 消费契约（不新增注入面 + 必须真进排序，Codex 2026-07-09 强化）

统一激活分只喂两个**已有**出口：①retriever 排序（决定哪些记忆进 prompt 的 memory 段，预算仍由 context_budget 管）②KB 检索排序（personal KB 四通道融合后的 rerank 因子）。**不复活 activation hints 注入区**——QKV 死因之一就是自建注入面。

**消费契约写死（验收级硬门，防重演 QKV"算了但不用"）**：统一 scorer 的输出**必须真正改变 retriever/KB 返回结果的顺序**，不允许"计算了 + record manifest + 返回原始顺序"。当前活标本必须一并修复：`invoker.py:411` 已 `record_activation_router_output(router_output)`，下一行 `:423` 却 `return _format_personal_kb_prompt_hint(candidates)`——用的是**原始 candidates 而非 routed output**。这是 QKV router 最大残留（算了不用）。验收判据：存在一个测试，构造两组 activation 分显著不同的输入，断言 retriever/KB **实际返回顺序随之改变**；record-only 不算通过。M8 的 score_trace 必须能证明"这条记忆的最终位次由激活分决定"。

## 5. 为什么这次是对的（与 QKV 逐点对照）

| QKV 层（收缩中） | 本设计 |
|---|---|
| 另建平行 router，与 ActivationScorer 双轨 | **升级 ActivationScorer 本体**，单轨 |
| Q 侧机械 regex 抽词，concepts 恒空 | Q = query + goal embedding + 会话工作集，全部真信号 |
| scoring 用词重叠模拟语义（L1 违规） | 语义判断留在 embedding/LLM（T2 keys、attention set），机械部分只做统计记账（频率/近因/图扩散——本就不是智能判断） |
| K 侧 SQLite 投影只写不读 | 复用 relation graph + lifecycle sidecar，每个写入都有读者 |
| hints 自建注入面，恒空渲染 | 不新增注入面，喂已有 retriever/KB 排序 |
| 无会话态，每 turn 无状态 | W_t 会话工作集，增量演化（KV-cache 式） |
| 无 eval 证据要求 | §6 eval 门前置 |

## 6. Eval 门（机制活 ≠ 效果好，效果证据先行）

上线前必须有读数（对应 eval-system-spec 的 growth report 体系）：
1. **离线对照**：用 T0 历史会话构造记忆召回评测集（某 turn 实际用到的记忆为 gold），对比 `纯 RRF` vs `RRF+BaseLevel` vs `RRF+BaseLevel+ContextBoost` vs 全量方程 的 R@k/MRR——分层归因每个组件的真实增益，任何无增益组件不上线（吸取 QKV 教训：45 提交零收益）。
2. **在线读数**：memory 段命中率（注入的记忆被后续回答引用的比例，score_trace 可测）、owner useful/misleading 反馈率变化。
3. **成本护栏**：每 turn 激活计算 P95 延迟 < 50ms（全确定性内存/单 SQL 操作，应远低于此）；TaskModulation 的 LLM 调用频率 ≤ goal 事件频率。

## 7. 不做的事（边界）

- 不做 ACT-R 噪声/检索阈值（概率召回损害可靠性，领域共识）。
- 不做每 turn 的 LLM 重排序/意图解析（成本与延迟不成比例；智能只在低频事件位）。
- 不做 Hebbian 边强化 v1（边权随共激活增强——诱人但无 benchmark 背书，列为 v2 候选，先靠 PPR 的节点级扩散）。
- 不动 Memory Gate/Platform Gate 写面（credit/工作集全是 sidecar/session 态，不触碰 T2/T3 语义真相）。
- 不建新的注入 prompt 区。

## 8. 分件清单（待拍板后并入 §7.2 全量清单，一次改完）

**M0 排在最前——eval 前置是纪律不是收尾**（Codex 2026-07-09）：M9 不能等 M5 之后"想起来"，否则重演 QKV"机制看起来活了但不知有没有收益"。先写离线 fixture + scorecard 的 **red test**（此刻纯 RRF 基线应产出一个确定分数），再动 scorer；每加一个组件，对照分必须真涨，不涨的组件不上线。

| # | 件 | 落点 | 验收 |
|---|---|---|---|
| **M0** | **离线 eval 对照集 + scorecard red test（先行门）** | eval 体系 | T0 历史构造 gold；纯 RRF 基线分先落地；scorecard 可跑（此刻就绿）——后续每组件必须相对基线真涨 |
| M1 | lifecycle sidecar 扩展最近 K 次访问时间戳 + credit 字段 | lifecycle_store.py / access_log.py | bump 写入环形数组；迁移幂等 |
| M2 | BaseLevel 公式替换 _usage_heat + 权重提级 | activation.py | 幂律衰减单测；suppression 优先级不变；**M0 对照 +BaseLevel 真涨** |
| M3 | FeedbackCredit 接线（heat_delta / activation_event.feedback.credit → sidecar credit） | session_feedback.py → lifecycle_store | useful/misleading 反馈后 credit 变化可查；只写 sidecar |
| M4 | Session working set W_t（增量演化 + 持久化 + 恢复；ACL 边界见 4.2） | runtime/session 态 + invoker 更新点 | 重启后 W_t 恢复；turn 衰减单测；**W_t 只含 ref/strength/ts 不含正文** |
| M5 | ContextBoost：W_t seeds → PPR → boost 因子 | retriever.py + relation_graph 复用 | 同 query 不同会话语境召回差异的行为测试；**M0 对照 +ContextBoost 真涨** |
| M6 | TaskModulation 确定性级（首次填 goal_terms/goal objective 双查询） | memory_service.py:208 填充 + retriever/activation + goal runtime | 有/无 goal 的召回差异测试（当前 goal_terms 恒空，此件是首次真接） |
| M7 | TaskModulation 智能级（attention set 声明，与 A7 共链） | Goal 对齐 A 系列 | goal 事件后 W_t 含声明种子 |
| M8 | 消费契约兑现 + 统一 score_trace（四项分解全落 reasons） | **invoker.py:423 改用 routed 结果** + activation.py + KB search | 激活分改变→retriever/KB 返回顺序**实际改变**的断言测试（record-only 不算过）；每条召回可解释分解 |

执行依赖：**M0 最先**（红测门）；M1→M2→M3 与 M4→M5 两条线可并行；M6 修 memory_service.py:208 的 goal_terms 空填 + M7 依赖 Goal 对齐 A 系列落地；M8 的消费契约兑现（修 invoker.py:423）是全量上线的最后硬门。与 §7.2 的关系：QKV 收缩（E2）先执行清场，本清单在干净地基上动工。
