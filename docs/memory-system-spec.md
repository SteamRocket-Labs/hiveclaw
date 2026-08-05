# Hive 记忆系统 — 实施规格

版本：v1.2（2026-07-02，修订记录见文末）
状态：**设计已闭环，据此实施。** 讨论过程、市面对标与现状证据见 `memory-architecture-rethink-2026-06-30.md`（过程档案，非实施依据）。效果 eval 的具体设计（§7.2）已定稿于 `eval-system-spec.md`。

---

## 0. 一页纲领

- **第一性**：记忆的形态由主体的形态决定。单 agent = 一个会长大的员工（持续存在、有自我认知、嵌在关系与领域、能力持续成长），不是问答机。
- **两平面**（从主体形态推出的必然）：
  - **侧写平面（会收敛）** = self + profiles + soul：对稳定实体的模型，越来越浓，写长了就是失败。
  - **知识平面（会成网）** = knowledge + milestones：领域知识，原子化 + 关系边织网。
- **三病根 → 三正解**：① 档案柜结构 → 两平面重组；② 缺主动收敛 → 收敛反思环（工序 4）；③ 没有"自我" → 一等的 `self.md`。
- **记忆 ≠ 能力**：记忆（认知）= soul/self/profiles/knowledge（+ milestones 叙事锚点）；能力（执行）= Skill/Sub-agent，住 `skills/`，从记忆长出但独立运行。生长链：`经历 → self → Skill → Sub-agent`。

**硬约束（实施不变量）**：
1. **MD 是唯一真相源**；SQLite 只存可从 MD 重建的派生索引；**不上 vector**。
2. **AI-Native L1**：一切智能步骤（提炼/分流/收敛/判定）由 LLM 在完整视野下做，不截断输入；机械处理只作可观测兜底。读取不跑 LLM（写入时已建好关系）。
3. **全程 governance**：语义写入过 Memory Gate（审证据/矛盾）+ Platform Gate（原子落盘）；soul 改动过 Soul Gate + owner 确认；Skill 过 eval/promotion。
4. **侧重点压 B（组织资产可治理）**：可治理、可审计、可迁移优先于单用户长对话召回。
5. 本规格聚焦**单 agent**；组织层 / 多 agent / ACL 是 Goal 2，显式后置。

---

## 1. 架构

### 1.1 三级概念：层 → 平面 → 文件（命名统一，全文一致）

- **T3（层）** = 记忆区。`soul.md` 在 T3 之上。
- **侧写平面**（收敛）= `self/self.md` + `profiles/*.md`。
- **知识平面**（成网）= `knowledge/<concept>.md` + `milestones/<milestone>.md`。

### 1.2 self vs soul：应然 vs 实然（不可合一）

- **soul = 宪法（应然、被赋予）**：我应该是谁、被要求的边界、必须达到的质量标准、服务谁。owner 定，agent 不可自改。frozen prefix（缓存锚点）。
- **self = 自传 / 体检报告（实然、长出来）**：我实际能做什么、怎么做、常栽哪。经历中自我观察，随时改。
- 关系：soul 是标准，self 是进度。self 稳定能力可**提名**上升 soul（工序 5）。
- 不可合一：变化速率差一个数量级 + prompt cache 边界（合一则每次自我更新击穿缓存）。

### 1.3 两套机制（四区其实不是四套）

- **侧写平面（self + profiles）**：收敛、operation patch、母题 + 场景条件、收敛蒸馏。**profiles 复用 self 全套机制**，仅养分来源不同。
- **知识平面（knowledge + milestones）**：成网、概念页 + 关系边、检索读。
- 反例：**不得用 self 的"收敛"去压 knowledge**，会误删知识节点——两平面蒸馏规律相反。

---

## 2. 完整链路：七道工序

| 工序 | 动作 | 频率 · 成本 |
|---|---|---|
| 0 | 经历发生 → T0 写入：逐事件 append `events.jsonl` + `source.md`，TURN_STOP 分段 | 实时 · 机械 · 零 LLM |
| 1 | 洗净打包 → T2：sealed 段整段喂 LLM 产 summary/labels/review；labels 顺带标 `self-signal` + 四区养分归类 + 重要性 | 回合级近实时 · 1 次 LLM |
| 2 | 分流标记：定该段升不升 milestones（判据 §3.5）、给四区各抽什么养分 → intake 候选进 heartbeat 待办 | 搭工序 1 便车 · 近零 |
| 3 | 批量消化 → T3：读累积养分，四区各产 operation patch → Memory Gate → Platform Gate | heartbeat 低频 · 批量 LLM |
| 4 | 收敛 / 反思环：全量重读某文件，消重/消矛盾/删过时/收敛变浓 | heartbeat/dream 定期 · 全量 LLM |
| 5 | 身份重固 → soul：self 稳定能力提名 + owner 批 | dream 极低频 · 高门槛 |
| 6 | 能力固化 → Skill：self 有效方法/规避提名 + 交接 Skill eval | 事件触发 · 交接能力层 |

工序 0-3 = 记录 → 提炼；工序 4-6 = 成长（病根二/三的正解）。

- **工序 1/2 的物理与逻辑**：工序 2 的判定（升级、养分归类、分流）物理上发生在工序 1 的同一次 labels call 内——两道逻辑工序、一次 LLM 调用。
- **跨段连续任务**：既有 **T2 续段合成器（stitch）** 保留，在工序 2→3 之间把跨段任务合成完整叙事，工序 3 消化时读合成叙事而非碎段。
- **观测点**：工序 1 单 call 承载多轴（labels / self-signal / 养分归类 / 重要性 / 分流）是省成本取舍——上线观测各轴质量，退化即拆多 call（拆分不改架构）。

---

## 3. 逐文件规格（职责 / 产生 / 验证 / 蒸馏）

**贯穿三机制（四区共性）**：① 产生 = T2 段包驱动 → LLM 产 operation patch（改写优先）→ gate；② 验证 = 来源 ref 回溯（见 §4.1）+ Gate 审证据/矛盾；③ 蒸馏 = 反思环（侧写收敛 / 知识织网治理）+ 引用计数 retention。

### 3.1 `soul.md`（T3 之上，冻结宪法核）
- 职责：拆纯后只留应然（Identity / 服务对象 / 边界 / 质量标准）。frozen prefix 注入。
- 产生：owner/HR 定；dream 仅身份级重大演化才动（工序 5，Soul Gate + owner 批）。
- 蒸馏：几乎不动；self 稳定能力提名上升需高门槛。

### 3.2 `self/self.md`（自我认知，侧写平面样板）

单文件、收敛、可读优先。四 section：能力 / 方法 / 失败模式 / 风格。

结构：
```markdown
# Self — 我对自己的认识
<!-- last_reflected: 2026-06-28 -->

## 能力
### 深度研究 — 熟练
拆解、多源检索、交叉验证。DeFi 尤稳。
- 边界: 纯学术综述弱
- 证据: t2-a1b2 · t2-c3d4 · fb-d45
- 锚点: [[ms-web3研报]]

## 失败模式
### 需求含糊时爱自己猜 — 规避中
触发: 目标不明确 / 多种解读。 规避: 先问一个澄清问题。
- 场景·深度研究: 高发,规避有效(避开 2 次) t2-a1b2
- 场景·写代码: 偶发,规避中 t2-i9j0
- 状态: 规避中(2026-06-20 起)
```

- **职责**：能力 + 熟练度（档位：熟练/一般/弱，不用数值）/ 方法 / 失败模式（带规避 + **生命周期状态 active·规避中·已根除**）/ 风格。
- **产生**：T2 labels 标 `self-signal`（近零成本）→ heartbeat 批量反思，产 operation patch（`op: add|update|retire` / `target` / `content` 自由散文 / `reason` / `evidence`）。add-vs-update 判定交 LLM。
  - **母题 + 场景条件**：一条 = 一个母题，母题下挂场景条件（同母题不同 regime 是变体，不是两件事）。三档：80% 重复 → update 加证据 confirm；~15% 微调 → 加场景条件行；~5% 真新 → add。拆分判据：某场景证据数 ≥ 阈值 **且** 触发/规避与母题主体显著背离，才 promote 独立母题。
- **验证**：来源 ref 回溯（禁凭空）；Memory Gate；**反例下调**（后续经历打脸则降级；负 feedback 是最强打脸）。
- **蒸馏**：dream/heartbeat 全量重读，合并重复 / 消矛盾 / 稳定能力上升 / 写长强制收敛。
- **读**：P0 常驻整份进 prompt，`active` 失败模式顶最前。

### 3.3 `profiles/{owner,collaborators,domain}.md`（收敛画像）
- 复用 §3.2 self 全套机制；养分 = 关于 owner/协作者/领域；owner 反馈（`session_feedback`）高权重。
- 边界 `domain.md` vs `knowledge/`：领域总体判断/品味 → domain（收敛）；具体概念事实 → knowledge（成网）。

### 3.4 `knowledge/<concept>.md`（知识网，复活扶正 wiki_curator）

```markdown
---
title: L2 Rollup
status: active
---
## Current Claim   L2 通过把计算移到链下、证明上链来扩容。
## Scope           以太坊扩容;不含 L1 分片。
## Evidence        t2-a1b2 · t2-g7h8
## Contradictions  早期以为 Optimistic 主流,t2-i9j0 显示 ZK 增长更快 → 已更新
## Relations       is_a [[k:Scaling Solution]] · contrasts_with [[k:Sharding]]
```

- 职责：一页一概念（原子），陈述性知识 + 关系边成网。
- 产生：工序 3 curator 判 update 现有页 vs 新建 → gate。**强制成网：新页必须 ≥ 1 条 `## Relations` 边**——边可指向尚不存在的页（前向引用，exists=False 节点，标记"值得写的页"），真无邻居的新概念不被卡死；禁的是连前向引用都不写的孤儿。
- 验证：低置信 claim 不覆盖 Current Claim（hold）；冲突进 Contradictions（不删旧）；来源 ref。
- **蒸馏 = 织网 + 治理（不是变短）**：防孤儿页（补关系/归档）、重复页（合并）、巨页（拆）。

### 3.5 `milestones/<milestone>.md`（里程碑锚点）
- 职责：少数里程碑，供 self/knowledge 当叙事锚点引用。核心是"选"不是"写"。
- 产生（重要性判据，工序 2 判——物理上搭工序 1 的 labels call，命中才升）：① owner 明确正/负反馈 ② 大失败/大返工 ③ 首次做成某类任务。
- **追认机制（判据④）**：被 self/knowledge 引为关键叙事锚点——该判据在工序 3 才能成立，靠追认补：工序 3 要给某条记忆挂叙事锚点而对应 T2 段未升级时，从 T2 追认升 milestones。堵"当时看不重要、后来才重要"的漏。
- 蒸馏：引用计数 retention（无人引的老锚点归档）。多数 T2 不升 milestones、抽完养分即归档。

### 3.6 T0 / T2（结构不变，立场与命名钉死）
- T0：`t0/sessions/<sid>/segments/<segid>/{events.jsonl,source.md}`，机械真相 + hash 链，resume/replay 源。
- T2：`t2/.../segments/<t2id>/{summary,labels,review,manifest}`，单段洗净打包，**证据主力（全量）**。既有**续段合成器（stitch package）**保留，角色见 §2 注。
- **T2 永不硬删**：引用计数 > 0 → 活跃；= 0 且超期 → 归档（压缩/冷存），**id 永远可解析**。retention 省的是热区体积，不是真相——"证据永不悬空"（§4.1）由此无条件成立。
- **命名纪律（一词一义）**："episode" 一词退出新层命名——T2 产物统一称 **segment package（段包）/ stitch package（续段合成包）**，里程碑区名 **milestones**。

---

## 4. 横切机制

### 4.1 来源 ref 体系（文件互联地基）

**两种来源，性质相反，必须分开：**
- **证据链** = 指不可变历史快照（`t2-` / `ex-` / `fb-`），发生即钉死永远在。**必须有，验证用。**
- **叙事锚点 + 活引用** = 指会变的东西（`[[ms-]]` 里程碑、`[[k:概念]]` 关系边）。可选，导航用。

- **id 规范**：`t2-<id>`（证据主力）· `ms-<id>`（milestones 叙事锚点）· `ex-<id>`（explicit）· `fb-<id>`（**不可变 feedback 记录**——session_feedback 行/T0 事件，记录内含其评价的 decision_id）· `[[k:概念]]`（记忆间活引用）。
- **硬原则：证据只指不可变源，绝不指 `ms-` 或活记忆条目**；叠加 T2 永不硬删（§3.6）→ 永不悬空**无条件**成立。
- **活引用完整性**：指条目稳定 id（`self:fm-爱猜` / `k:l2-rollup`）不指内容；收敛合并时保留主 id、次 id 留 tombstone → 主 id；SQLite 反向索引维护。
- **SQLite**：正向 ref 在 MD，反向索引 + id 解析在 SQLite（可从 MD 重建）。**反向索引 count = 引用计数 = retention 依据。**

### 4.2 读侧：侧写常驻 + 知识检索

- **常驻（P0，不检索）= soul/self/profiles + explicit overlay 活跃条目**：小且永远相关，整份进 prompt，frozen prefix 附近，self `active` 失败模式顶最前（overlay 见 §4.4，被吸收后退出常驻）。
- **检索（按 query top-k）= knowledge/milestones**：PPR 多跳捞相关概念页子网 + 里程碑。
- **读取不跑 LLM**（写入已建关系）。
- **预算**：侧写占固定额度、知识填剩余；额度数值归 config（平台配置），spec 只定机制——**侧写超预算 = 写侧收敛失败告警**（触发工序 4），不硬截。

### 4.3 成长机制（工序 4/5/6）

共性：**提名 + gate，门槛随影响面递增**；都在 dream/heartbeat 反思时触发；保留来源链。

- **收敛环（工序 4）**：与写入相反 —— 写入是增量 operation patch，收敛是**全局整份重写**（消重/消矛盾/删过时）。触发 = 脏度（patch 数/字数/矛盾超阈值）+ 节律兜底。输入喂**完整文件不截断**（L1）。输出整份改写 + 收敛说明，过 gate，**旧版存档可回滚**，合并条目 source_refs 取并集。载体：脏度触发跑 heartbeat、节律兜底跑 dream（dream 从只碰 soul 扩展到碰 T3）。安全重点 = 防误删。
- **self → soul（工序 5）**：提名 + owner 批（**非 agent 自改 soul**）。触发 = 某能力长期稳定 + 高置信 + 无反例下调。
- **self → Skill（工序 6）**：软 → 硬转写 + **交接**（记忆只产 Skill 候选，交能力层既有 eval/promotion，非记忆直造）。固化后 self 那条不删，标 `已固化 → [[skill-x]]` 双向链。

### 4.4 直接输入通道（explicit + feedback）

绕过经历、要立即生效的两类用户信号，**不新增区，是汇入四区的快入口**。

- **explicit（"记住 X"）**：立即写 `explicit/` overlay + 当轮 P0 激活 → 工序 3 分流进四区 → 分流后 overlay 条目 `absorbed → [[目标]]` 退役（与四区写入**同一次原子交接**，防双重注入）。分流到多区的条目按目标分别标 absorbed，**全部目标落盘才整条退役**，未过 gate 的目标下个 tick 重试（期间短暂双注入有界、可接受）。时效性条目留 overlay 待过期。
- **feedback（"错了 / 做得好"）**：不独立成条，挂 `decision/<id>`；负反馈当轮即标结论"已被 owner 否定"；工序 3 作高权重极性信号 —— 正 → 强化（能力 confirm↑、逼近 Skill 固化）；负 → self 触发失败模式 + knowledge 进 Contradictions；直接喂 self 反例下调。

### 4.5 治理

语义写入过 Memory Gate + Platform Gate；explicit/feedback 过 write gate（sensitivity/privacy，PL4 拒）；soul 过 Soul Gate + owner；Skill 过 eval。组织层"谁能注入"ACL 后置（Goal 2）。

---

## 5. 产物清单

```
memory/
  soul.md                                     身份宪法核(应然,冻结)
  self/self.md                                自我认知
  profiles/{owner,collaborators,domain}.md    收敛画像
  knowledge/<concept>.md                      知识网(原子页 + Relations)
  milestones/<milestone>.md                   里程碑锚点
  explicit/                                   直接输入 overlay(快通道,非区)
  t0/sessions/<sid>/segments/<segid>/{events.jsonl,source.md}   机械真相(不变)
  t2/sessions/<sid>/segments/<t2id>/{summary,labels,review,manifest}   单段打包(不变,证据主力)
  indexes/{wiki_map.md, index.sqlite}         派生索引(反向 ref/引用计数/id 解析,可重建)
  control/                                    sidecar(consolidation-debt / retention 台账)
— 能力层(非记忆) —
skills/<skill>/                               Skill capsule(instructions/refs/templates/scripts/evals/subagent)
```

固定收敛文件 = soul + self + profiles×3 = 5 个；knowledge(N) / milestones(M) 随经历增长。旧 `t3/{episodes,user,worker,capabilities}.md` 四文件 → 本结构（映射见附录）。

---

## 6. 实施计划

### 6.1 优先级
**C9 雨天地基最先 → 读侧 → knowledge → profiles/milestones**。C9 先行：三断层小、是地基，新管线工序 1-3 全部踩在 T2 job 上，不修则经历静默丢失、越建越亏（第一轮"三断层先于新功能"决议的回归）。之后先定读侧（"怎么用"反哺写侧形态，省返工），profiles 复用 self、milestones 只定判据最后收。来源 ref 体系（§4.1，依赖 C8 反向索引最小集）**先于 C7 迁移落地**。

### 6.2 雨天地基 C9（三断层，一次完整 pass）
1. **T2 held/failed 重试 + 崩溃恢复**：现状 held 是"valid terminal state"、无人捡回（`segment_package.py`）。加 startup + heartbeat 级 sweep（复用既有 recovery/resume 模式），held/failed job 有界重试、超限告警——消灭"经历静默进不了记忆"。
2. **T2→T3 停滞告警**：consolidation-debt 台账（`control/` sidecar + C8 表）超阈值 → 观测面告警（admin/监控），不静默积压。
3. **retention 落地**：SQLite 反向索引引用计数（§4.1）+ 归档执行——**永不硬删**（§3.6 立场），规则表见 §6.5。

### 6.3 迁移 C7（一次完整 pass，含 legacy backfill）
- **soul 拆纯四件套**：① 精确边界（soul 留应然、self 收实然）② 迁移脚本 + backfill（旧 soul 实然部分抽入 self 初版）③ HR 模板同步（新 agent soul 只生成应然、self 给骨架）④ dream 逻辑改（soul 仅身份级动）。
- **四文件重组**：user→profiles；worker 的 constraint **分两种（LLM 判，§3.2 那把刀）**——被赋予的硬约束→soul、自察的能力软边界→self，blocked_pattern→self；capabilities 的 strategy/reference/general→knowledge / 能力自评→self；episodes.md→milestones/（多数留 T2）。
- **平台配套同步（同一 pass，不留债）**：① `HEARTBEAT.md`/`DREAM.md` SOP 模板重写 + 各 agent 克隆模板同步（历史坑）；② `memory-vault-path-contract` 文档与 `hygiene.py` 认新路径（旧 `t3/` 四文件迁移后的隔离规则）；③ `relation_graph._PAGE_DIRS`（现 `("wiki","scenes")`）改指 `knowledge/`+`milestones/`，`wiki_map.md` 更名/改指向。
- 施工顺序：self 读写先跑通（非 breaking）→ soul 拆纯迁移（breaking）→ 同一验收。**非 MVP 分期。**

### 6.4 SQLite schema C8
派生表：反向 ref 索引（来源 → 引用它的条目）、id 解析（短 id → 路径/内容）、引用计数（retention）、consolidation-debt 台账、复合标签（分轴）。全部可从 MD 重建。

### 6.5 施工细节（方向已定）
冷启动（新 agent 四区空、self 给初始骨架）；读侧 query 种子取值；工序 2→3 的 intake 队列（control/ sidecar，可从未消费的 T2 labels 重建）；retention 完整规则表（对标 Letta：引用 3+ 晋升 / decisions 永不过期 / debug 类短期归档等——**规则只管归档节律与热区，不含硬删**，§3.6）。

---

## 7. 验收

### 7.1 设计不变量（每次改动自检）
- 所有文件产出有明确 writer（无孤儿）。
- 侧写只收敛、知识只成网（不混）。
- 一切智能步骤 LLM 全视野、读取不跑 LLM。
- 证据只指不可变源 + T2 永不硬删（永不悬空无条件）；活引用有 tombstone。
- 语义写入全过 gate；soul 改动过 owner；durable 写不绕过 gate。

### 7.2 效果 eval（North Star）
设计闭环 ≠ 效果达成。实现后需 eval 证明记忆真让 agent 越来越强，**对标 hermes-agent**（外部行为 eval 前不宣称超越）。方向：同类任务重复出现时，self 失败模式规避生效率上升、knowledge 复用命中率上升。

**eval 具体设计已定稿于 `eval-system-spec.md`（2026-07-02）**：其 §2.1 成长报告即本节承诺的最小骨架——指标计算（失败模式复发/规避按 self.md 失败模式 id 聚合、复用命中率、反馈极性、返工率）、语料形态（真实工作流本身，不造语料）、复发检出（工序 1 labels 带失败模式 ref）。仍然有效的纪律：不得在无靶子状态下施工完即宣称效果。

---

## 附录：旧四文件映射

| 旧 T3 文件 | 拆去向 |
|---|---|
| `user.md` | `profiles/owner.md`（+ collaborators） |
| `worker.md` | constraint 分两种（LLM 判）：被赋予的硬约束 → `soul.md`、自察的能力软边界 → `self/self.md` 能力边界；blocked_pattern + 能力自评 → `self/self.md` |
| `capabilities.md` | strategy/reference/general → `knowledge/`；capability 自评 → `self/self.md` |
| `episodes.md` | `milestones/<milestone>.md`（里程碑）；多数留 T2 归档 |

---

## 修订记录

- **v1.2（2026-07-02）**：§7.2 效果 eval 最小骨架定稿，指向 `eval-system-spec.md`（成长报告 = 同一件东西）；头部状态行同步。
- **v1.1（2026-07-02）**：① T2 立场钉死——永不硬删，引用 = 0 只归档、id 永远可解析，"证据永不悬空"改为无条件（§3.6/§4.1/§7.1）；② 雨天三断层（held 重试 / 停滞告警 / retention）回列为 **C9**、先于新功能（§6.1/§6.2）；③ 里程碑区 `episodes/` 更名 **`milestones/`**（id `ms-`），"episode" 一词退出新层命名，既有 T2 续段合成器角色补明（§2/§3.5/§3.6）；④ **追认机制**补录（§3.5 判据④）；⑤ 勘误批：读侧补 explicit overlay（§4.2）、强制成网允许前向引用（§3.4）、`fb-` 语义澄清（§4.1）、工序 1/2 物理与逻辑 + 工序 4 载体（§2/§4.3）、explicit 部分失败退役规则（§4.4）、侧写预算归 config（§4.2）、labels 多轴观测点（§2）、C7 平台配套三项（§6.3）、worker constraint 映射分两种（§6.3/附录）。效果 eval 具体设计定为下一轮专门讨论（§7.2）。
- **v1（2026-07-02）**：初版，自 `memory-architecture-rethink-2026-06-30.md` 讨论稿收敛。
