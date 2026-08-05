# 记忆系统重审 — 讨论稿

日期：2026-06-30（2026-07-01 追加第一性推导层 §0.5 / 现状校准 §1.5 / 方案骨架 §9）

状态：**结论已收敛至实施规格 `memory-system-spec.md`（v1, 2026-07-02）。本文降为过程档案 —— 记录讨论如何一步步想通、以及市面对标与现状证据，不作实施依据。** 实施对着 spec，不对着本文。

阅读顺序（2026-07-01 后）：**§0.5 是纲**（从"主体形态"第一性推导，回答"为什么、往哪走"）；§1-§8 是证据（现状 + 对标，回答"现状如何、市面如何"）；§1.5 校准 §1 的一处盲点；§9 把纲落成完整目标架构（含 T0→T3 每文件四问 + 两个判断点的建议）；§10 起逐组件下沉到字段 / 机制级实施设计。纲与证据冲突处，以纲的目标为准。

参与：Owner + Claude（4 个现状调研 agent + 4 个市面对标 agent 取证）。

---

## 0. 背景与目标

底层已完成 CCPlus 化，session 体系（checkpoint / hook 生命周期 / recovery）改造闭合。现在在此基础上**重新审视整个记忆系统**。

要回答的问题：
1. 记忆系统有没有断层？
2. T0→T2 当前以什么为基点？是否应改成"以意图最小循环为点、hook 实时写入"？
3. 长期 / 短期 / 瞬时记忆分别该放在哪、怎么搭？
4. 作为 **Agent-as-a-Service 企业中台**，记忆的**侧重点**在哪？据此评估 Basic Memory Framework 是否要改。

硬约束（Owner 定）：
- **Basic 一定要轻量**：不做向量化 / embedding（太重），**最多上 SQLite**。理由：CC 底层也轻，Basic 要轻。
- 允许选一个记忆系统作为**可选增强项**（非 Basic）。

---

## 0.5 第一性推导：主体形态决定记忆形态（2026-07-01 Owner 追加 —— 本轮讨论的纲）

> §1-§8 是自下而上（从现状 + 对标推）。本节是自上而下（从"agent 作为主体"的第一性原理推）。**先想清楚主体，不能上来就设计文件——记忆的形态由主体的形态决定。** 下面所有文件结构 / schema / 机制，都应是这条链的落地，而不是反过来先定文件再凑逻辑。

### 0.5.1 主体是单 agent —— "单 agent"是什么形态？

一次 LLM 对话无状态、一次性：答完即忘，今天的它与明天的它无关。"agent 作为主体"意味着它是**持续存在的实体**——跨会话、跨时间，今天的它和明天的它是"同一个"。这个同一性靠什么维系？只能靠记忆。**记忆是主体之所以成为主体（而非一串互不相干对话）的根据——第一性的，不是功能之一。**

这个持续存在的实体有五个不可省的侧面：
- **自我**：我是谁、我擅长什么、我的边界、我常犯的错、我的做事风格。不是"知道很多事"，是"知道自己"。
- **关系**：我为谁工作（owner）、我和谁协作。有位置的智能，不是悬空的。
- **领域**：服务的行业 / 主题，理解越深越有价值。
- **经历**：发生过什么，是一切的证据与原料。
- **越来越强**：原生 agent 的定义就是单体能力持续成长。

一句话：**单 agent = 一个持续存在、有自我认知、嵌在关系与领域中、且能力持续成长的数字实体——一个会长大的员工，不是一个很强的问答机。** 这个区别决定一切。

### 0.5.2 这样的主体该有什么记忆？两个平面（推导的必然，非从方案挑选）

五个侧面按"变化速率 + 本质"自然分成两个平面：

- **侧写平面（会收敛）**：自我、主人、协作者、领域——本质都是"对一个稳定实体的模型"。规律是**收敛**：越来越准、越来越浓缩。好的主人侧写不是"记下主人说过的每句话"，是"越来越懂主人"，一幅被不断改写提炼的活画像。**侧写写长了就是失败。**
- **知识平面（会成网）**：领域里一条条的知识——本质是**关联成网**。价值不在单条，在条与条之间的关系。它无限增长，所以必须**原子化（一条只讲一件事）+ 靠关系边织网**，否则就是一堆再也捞不出来的碎片。

- **经历（episodes）** 是两平面的**原料与证据流**，本身不是目的地——向上提炼成侧写的更新、知识的新节点。
- **soul** 是侧写平面里最稳定的那个核（身份）。

**关键**："侧写 vs 知识"这个二分不是从某个方案里挑的，是从主体形态推导的必然。会收敛的实体模型 和 会成网的知识节点，混在一起（Hive 现在正是如此），关系永远理不清；只有分平面，"用知识图谱的逻辑看、关系结构非常清晰"才做得到。

### 0.5.3 根本问题：三个病根（不是病症）

§1.4 的断层清单（held 不重试、读侧视野截断……）是**病症**。往下是**病根**：

- **病根一 —— 用"归档"的结构装"主体"的记忆。** 现在 T3 四文件（episodes / user / worker / capabilities）按内容类型扁平分类，是**档案柜思维**。但主体记忆不是档案，是"活的自我 + 活的网"。用档案柜装成长型记忆，必然：侧写与知识混装、自我无处安放、知识之间没关系、越存越乱。**结构性错配，调参救不了。**
- **病根二 —— "记录型"而非"成长型"，缺主动收敛。** 成熟系统都在做减法（合并 / 提炼 / 去重），质量 > 数量。Hive 机制上支持改写（replace / retire），实际是**被动增量**：新 T2 来了往 T3 加，天然 append。缺一个"停下来、重读整个自我 / 主人侧写、消矛盾、提精华"的**反思环**。人靠睡眠和反思做这件事；Hive 的 dream 只碰 soul、不碰 T3，这一环缺席。没有收敛，"越存越多" = "越来越钝"。
- **病根三（最要命，正对着"越来越强"）—— 没有"自我"。** Hive 有冻结的 soul（我是谁），却没有动态的 self（我现在能做什么、上次栽在哪、我的方法是什么）。一个不知道自己"这类任务上次错哪"的 agent，每次从零开始，不可能越来越强。**self-model 的缺失，是"越来越强"这个本质定义的正面缺口。**（起点不是字节级全空，见 §1.5 校准；但"活的、一等的自我"确实缺席。）

### 0.5.4 最终目标

**让单 agent 拥有一个"活的自我模型 + 一张活的知识网"，使它作为持续存在的主体，每一次经历都让它对自我、对主人、对领域的理解更进一步——这才是真正的"越来越强"。**

### 0.5.5 MD 约束的深层理由（升级 §0 硬约束）

MD 是真相源、SQLite 只做索引（可从 MD 重建）、不上 vector——深层理由不只是"CC 也轻"：
- **记忆应归主体所有**：像人的笔记本，主体自己能读、能改、能带走，而不是锁进要专门基础设施才能解读的向量黑箱。这跟控制中台定位（可治理、可审计、可迁移）本就是一回事。
- **不上 vector 不是砍功能**："写入时让 LLM 建立显式关系"本就比"检索时靠向量相似兜底"更 AI-native——**用写入时的智能，换检索时的极简。**

---

## 1. 现状（基于当前 branch 代码调研，evidence-first）

### 1.1 T0→T2→T3→soul 链路

- **T0**：实时逐事件 append（`memory/t0/ledger.py:95 append_t0_session_event`），每条 message 即时写 `events.jsonl`（机械真相 + hash 链 + fsync/flock）+ `source.md`（投影）。非批处理。
- **分段（关键位移）**：分段主边界现在是 **`TURN_STOP`**（`hooks_setup.py:435 _t0_turn_stop`），**turn ↔ segment 1:1**——一个 segment = 一个已完成的 user turn（一次 invoke_agent）。`SESSION_IDLE/CLOSE` 已被降级为 `*_fallback`。**这是本轮 ccplus 改造的连带位移**（改造前分段单元是 SESSION_IDLE 空闲间隔，可能跨多 turn）。
- **T0→T2（回答问题 2）**：以 **sealed T0 segment（=1 个 user turn）** 为基点，seal 后由 `_build_t2_for_sealed_segment`（`hooks_setup.py:590`）→ `run_t2_segment_package_job`（`memory/t2/segment_package.py`）一次性整段喂 LLM，产出 summary.md / labels.md / review.md（3 个 LLM agent 串行，`max_tokens=8192`），过 Platform Gate。**事件驱动、回合级近实时，不靠 daemon 扫描；既不是 session 批处理，也不是 compaction checkpoint 基点。** 唯一"批处理"残留 = 段内一次性（非增量）——但这保证了完整视野，应保留。
- **不阻塞用户**：web 主路径 T2 构建在后台 run 尾部 `await`（fire-and-forget 思路），用户响应已先返回。
- **T2→T3**：heartbeat curation（`services/heartbeat.py`，`HEARTBEAT_TICK_SECONDS=60`、`HEARTBEAT_DEFAULT_INTERVAL_MINUTES=120`），把"T3 Consolidation Job Ready"呈现给 heartbeat agent，agent 主动 pitch（软泵）。
- **T3→soul**：dream（`services/auto_dream.py`，24h + 3 sessions 或 2 ticks 门控）。
- **legacy 已下线**：`extract_agent.py` / `t2_store.py` / `memory/learnings/` 仅剩 backfill/migration 用途，无 runtime 热路径。

### 1.2 "checkpoint" 一词已 5 义重载（讨论时必须指明是哪个）

| # | 含义 | 落点 | 能否当"意图边界" |
|---|---|---|---|
| 1 | recovery checkpoint（persist-before-tool） | `engine.py:1405` / `recovery_manifest.py` | 否（每 tool call 一次，太碎，纯技术恢复） |
| 2 | permission checkpoint | `PermissionCheckpointV1` | 否 |
| 3 | compaction checkpoint | `CompactionLifecycleV1` | 间接（PRE_COMPACTION 会 seal T0） |
| 4 | **T0 `checkpoint_kind`** | `ledger.py:871` | 是——T0 真正的语义分段载体（`user_turn_stop`/`turn_abort`…），但标的是"结束" |
| 5 | **workspace checkpoint** | `session_workspace_snapshot.py` / `web_chat_runtime.py:933` | **是——以 user-message event_id 为锚，现服务 rewind/branch，天生是"意图起点"** |

### 1.3 intent 现状

`intent_id` 存在（`invoker.py:236`）但**退化为 turn_id**（种子取自 turn_id/request_id/message_id）。**没有跨多 turn 的"意图跨度"概念**。turn / intent / turn_envelope（`runtime/turn_envelope.py`）已是一等概念并贯穿全链。

### 1.4 断层清单（回答问题 1）

**主链晴天路径全接通**（触发方 live、门控合理、读侧消费、去重工作、legacy 下线）；**本轮 ccplus 改造未给记忆引入新断层**，PRE_COMPACTION 先封 T0 再建 T2 反而强化了压缩前证据保全。断层集中在**失败路径 + retention**：

| 级别 | 断层 | 位置 | 影响 |
|---|---|---|---|
| 🔴 | **T2 held/failed 永不重试、崩溃无恢复** | `segment_package.py:272/339/425`；`main.py` startup resume 列表无 T2/T3 | 租户缺 summary model 或 LLM 持续失败 → 该回合 T0 证据**永久进不了 T3 = 永久丢记忆**。历史 summary-model TypeError 事故即此类。**唯一会永久丢记忆的硬断层。** |
| 🟠 | **T2→T3 软泵无停滞告警** | `heartbeat.py:586 _read_pending_t3_intake` | heartbeat 模型弱/忽略 pending → T3 无限期停滞且系统不自知。动摇 Goal-1 自进化连续性。 |
| 🟡 | **新 T2 无 retention** | 新 `memory/t2/sessions/**` 无归档；`auto_dream.py:2063 _truncate_t2` 操作的是旧空 store | 无限膨胀 + 漂移。 |
| 🟡 | 读侧 fail-soft 静默掉记忆 | `memory_service.py:129/154` | principal 未解析/检索异常 → 该轮 T3 记忆整体抑制，调用方拿空串照常继续（有 metric，用户无感）。 |
| 🔵 | legacy 残留/增噪 | dream 维护 legacy logs、`enhancement.py` 字段契约不一致被吞、`heartbeat.py:2062` 孤儿 | 非丢失，卫生问题。 |

---

## 1.5 现状校准：被降级的"知识成网"支线 + 自我的雏形（2026-07-01 补，修正 §1 盲点）

§1 的现状调研聚焦 T0→T2→T3→soul 主链，**漏了一条与 §0.5 直接相关的支线**。Claude 重新核实当前代码（evidence-first）：

**① 知识平面（成网）不是空白，而是"造好过、又被主动降级"。**

Hive 曾经有一套相当完整的"知识成网"实现：
- `memory/wiki/<concept>.md` 概念页（`## Current Claim / ## Scope / ## Evidence / ## Contradictions / ## Retrieval Tags`）—— 原子化知识节点 + 矛盾追踪。
- `memory/scenes/<slug>.md` 情节页。
- `[[wikilinks]]` + `## Relations` typed edges —— 关系边。
- `relation_graph.py` + `personalized_pagerank`（HippoRAG 风格多跳传播）—— 关系网检索。
- `understanding_store.py`：`subject — relation → object` + `contradiction_history / confidence / last_confirmed_at / open_questions / boundaries` —— 近乎教科书的知识关系条目。

**这套东西现在的状态**：`wiki_curator.py` / `scene_curator.py` 头部明写 **Deprecated**，`understanding_store` 的 `record()` / `contradict()` **已 raise 禁用**，注释统一为 "accepted T3 truth is now restricted to the four files"。写侧 curator **无 live 调用者**（grep 证实，仅 migration / eval 用）。只有 `relation_graph` + PPR 检索器还活着，`retriever.py:259 _retrieve_wiki_pages` 读侧仍会捞 wiki 页——**但源头已无 live writer 生产，读的是遗留页 + 前端只读视图**。

**根因反转（重要）**：病根一"档案柜结构"不是"从没想到成网"，而是**一次"为了 T3 单一真相源，把成网设计收敛成扁平四文件"的过度收敛的产物**。当初大概率因为"多个语义真相源打架"（wiki 想当 truth、understanding 想当 truth、T3 也想当 truth）被迫二选一，于是全砍成四文件。**§0.5 的"两平面"洞察恰好消解这个矛盾**：侧写和知识本就是两种不同的真相，各自单一即可，不必挤在一个 `t3/` 里打架——不是"wiki vs T3 二选一"，是"侧写平面 + 知识平面 各司其职"。这把方案从"发明"变成"修复过度收敛 + 扶正已有零件"。

**② 自我（病根三）不是字节级全空，但作为一等公民确实缺席。**

诚实校准 §0.5.3：
- `t3/worker.md` 的 categories 含 `constraint` 和 `blocked_pattern` —— "我的边界"和"我常犯 / 被阻塞的模式"的**雏形**已在这里。
- `understanding_store` 的 `boundaries / open_questions / contradiction_history` 字段设计 —— 是自我模型的**理想蓝图，但被禁用**。

但它们：混装在"worker 画像"里、是 **append 不收敛**、且部分被禁用。**作为"活的、会收敛改写、被读进 prompt 驱动变强"的一等自我模型，确实缺席。** 洞察成立；只是起点不是零，是"雏形散落 + 被降级"——这让方案从"发明"变成"扶正 + 收敛"（见 §9）。

---

## 2. 市面 SOTA 对标（7 家，evidence-first）

> 全部经实时检索取证；benchmark 数字均为**厂商自报**，标注来源。

### 2.1 三个硬事实

**① "SOTA benchmark 数字"基本不可信，别追。** 同一 LOCOMO 测试集，各家自报互相矛盾：

- Zep 自报 **84%** → Mem0 重算指控虚高、实为 **58.44% ± 0.20**（称 Zep 分母排除对抗性 Category-5 却分子仍计其正确答案，约 25.56 点虚高）→ Zep 自辩 **75.14% ± 0.17**（J score）。来源：`github.com/getzep/zep-papers/issues/5`。
- Mem0 v3 自报 **92.5%**；Cognee 自报 HotpotQA **0.93**（仅 24 题，自认"非独立研究"）。
- **最狠**：Mem0 自己的数据显示其系统**被"全文塞进上下文"的 full-context baseline（~73% J）超越**（Mem0 best ~68%）。来源：Zep blog "lies, damn lies, and statistics"（`blog.getzep.com`）。
- **结论**：这些系统的价值主要在 **token 成本/效率**，不在召回质量；**别为追某家分数改架构**；符合 Hive 铁律（外部独立 eval 前不认"超越"）。

**② 几乎所有 SOTA 都重度绑 vector/graph。** Mem0=vector(+可选 graph)、Zep/Cognee=knowledge graph、A-Mem（连"agentic memory"都）=ChromaDB+MiniLM。**没有一个轻量的能上 LOCOMO 榜**——因为该榜测的就是向量检索擅长的"长对话事实召回"，二者共生。

**③ MD/非向量路线对单用户"难超越"，只在我们的场景才失效。** 来自竞争对手 Zep 博客原话：

> "生产环境里一些最强的 agent 就用**纯 markdown 文件**存记忆，**对单 agent、单用户，这几乎无法被超越**。这个模式只在可预测的地方失效：**规模化时、事实变化与错误累积时、以及并发多 agent 时。**"

它失效的三点——**规模、事实冲突/演化、并发多 agent**——**逐字就是"企业中台"的定义**。

### 2.2 各家速览 + 可借鉴点

| 系统 | 范式 | 存储依赖 | benchmark（自报） | 最值得借鉴 |
|---|---|---|---|---|
| **Mem0** | LLM 提取 + scoped memory | vector(+可选 graph) | LOCOMO 自报 92.5%（争议） | scoped memory（user/session/agent 作用域） |
| **Cognee** | ECL(Extract-Cognify-Load) + KG | graph + vector | HotpotQA 0.93（24 题） | pipeline 化的 extract→图谱 |
| **Letta (MemGPT)** | OS 式分层 + self-editing | 可 SQLite/Postgres（不强制 vector） | DMR 起源 | **分层 core/recall/archival；引用次数过期；复合标签；metacognition 块；archival 索引；sleep-time 用便宜模型** |
| **Zep (Graphiti)** | temporal knowledge graph（bi-temporal） | graph db | 见 §2.1 争议 | 时序/事实演化建模（谁在何时改了什么） |
| **A-Mem** | agentic，Zettelkasten 动态链接 | ChromaDB + MiniLM | LOCOMO v1 | **记忆之间动态建链 + note 随新记忆演化**（≈我们 wikilink T3） |
| **LangMem** | 记忆类型学 | 无强制（可插） | — | **语义/情节/程序三分 + hot-path/background 双通道写入** |
| **Generative Agents** | memory stream + reflection | 无强制 | 行为可信度（TrueSkill Full 29.89 > 人类 22.95） | **reflection（观察→高层洞察）+ recency(0.995 decay)/importance(1-10)/relevance 三因子打分检索** |

### 2.3 轻量可借鉴清单（不依赖 vector，可落地）

- **Letta 引用次数过期**：session context 30 天、被引用 3+ 次晋升长期、decisions/preferences 永不过期、debug 14 天（除非 tag `type:root-cause`）、TODO 90 天复核。→ 补我们的 retention 断层，比纯时间衰减聪明。来源：`forum.letta.com/t/sleeptime-agents-for-memory-consolidation-best-practices-guide/154`。
- **Letta 复合标签分轴**：`project:` / `type:` / `tech:`，按任一轴过滤，而非拼成一个串。
- **Letta metacognition 块**（后台写）：记录 agent 盲点 / 检索缺口 / **consolidation debt**。→ 补我们"T2→T3 停滞无告警"断层。
- **Letta archival 目录索引块** = 我们的 `wiki_map` 思想。
- **Letta 便宜模型整合**：primary 用 Sonnet/GPT-4o，sleep-time 整合用 Haiku。→ 对应 heartbeat/dream。
- **LangMem 三类记忆 + hot/background 双通道**：语义/情节/程序；即时提取 vs 后台整合。→ Hive 已天然具备（见 §4）。
- **Generative Agents reflection + 三因子打分**：→ Hive 已有 dream/heartbeat consolidation + retriever 打分。

---

## 3. 侧重点判断：压 B（组织资产可治理）

**两条路线**：
- **A — 个体越用越聪明**：单 agent 通过记忆自我进化，个体智能/长对话召回最大化。（Mem0/Letta/Zep 拼 benchmark 的方向）
- **B — 组织资产可治理**：知识不因某 agent/员工流动而丢失，跨 agent 跨 session 沉淀为公司资产，全程权限可控、可审计、可回滚。

**判断：Basic 重心压 B。** 三条理由：
1. A 是**红海 + 绑 vector + 与轻量约束天然冲突**，且 benchmark 造假，性价比极低；
2. B 是**市面空白**——7 家没一个认真做多租户隔离/权限 ACL/审计/组织级沉淀，全是单 user 单 agent；
3. B 恰好是 **MD-first 失效、需要真本事的那条轴**（scale/冲突/并发多 agent），也恰好是 Hive 已有地基（多租户 RLS + Memory Gate/Platform Gate + T0/T2/T3），且被竞争对手 Zep 金句直接背书。

**取舍（明确）**：主动**放弃**"单用户长对话召回的 LOCOMO 排名"，接受这一项不如 Mem0/Zep 的向量方案；**换取**别人没做的组织级治理记忆。系统不追求 100% 完美——这是主动取舍。

---

## 4. 长/短/瞬时记忆架构（回答问题 3）

Hive **已有分层**，只是没用"长短瞬时"的语言。对标 Letta（core/recall/archival）+ LangMem（语义/情节/程序）：

| 层 | 市面对标 | Hive 现状对应 | 缺什么 |
|---|---|---|---|
| **瞬时 / working** | Letta core memory（常驻 context 高优先块） | 当前 turn 的 api_messages + session working context | 缺**显式的、agent 可自编辑的常驻块**（Letta self-editing core） |
| **短期** | Letta recall memory | session 内 + 近期 T2 turn-segments | 基本够 |
| **长期** | Letta archival memory | T3（episodes/user/worker/capabilities）+ soul | 检索/关系在 scale 下弱（→ 加 SQLite 索引） |
| **记忆类型** | LangMem 语义/情节/程序 | 语义→T3 user/worker；情节→T3 episodes + T2；**程序→Skill 系统** | 已天然三分；程序记忆用 Skill 承载比 LangMem 更清晰 |

真正要补：① 瞬时层的显式可自编辑常驻块；② 长期层的检索/关系（见 §6 SQLite）。

---

## 5. 意图定义 + 实时写入（回答问题 1、2）

**意图定义（Owner 定，采纳）**：**用户输入为起点、Agent 最终交付为终点 = 一段完整意图，和 Branch/Rewind 边界一致。**

**现状已满足大半**：
- workspace checkpoint（#5，以 user-message event_id 为锚，现服务 rewind/branch）**天生就是"意图起点"**；`TURN_STOP` 就是"最终交付"终点。
- T0→T2 **已在按"意图最小循环"实时写入**（回合级、fire-and-forget 不阻塞）——这块不用新建。

**唯一待决 = 跨多轮意图**：建议**不改 T2 分段**（turn 级机械边界可靠），让 **T3 做跨 turn 的意图聚合**（本就是 T3 consolidation 的活；意图边界判断是 L1 智能问题，放 T3 让 LLM 做最合适）。理由：在 T2 引入"意图状态机"会把可靠的机械边界换成易错的智能判断，且要改增量 T2 数据模型，复杂且脆。

---

## 6. Basic 改不改 + 增强项（回答问题 4 的落地）

**Basic：保持 MD 为真相源 + 加一层 SQLite 作"索引/关系/标签/引用计数"（不是 vector）。** 在"最多 SQLite"约束内，补 MD 在 scale 下的短板：
- SQLite 存**可重建的派生索引**：复合标签（分轴）、**引用计数**（按引用而非年龄过期）、relations（wikilink 结构化）、tenant/agent 索引、consolidation debt 台账。
- **MD 仍是 truth，SQLite 崩了能从 MD 重建。**

**同时优先修三个断层**（比任何新功能优先）：
1. T2 held/failed 重试 + 崩溃恢复（复用刚建好的 recovery/resume 模式，给 T2 job 加 startup/heartbeat 级 sweep）；
2. T2→T3 停滞告警（Letta metacognition / consolidation-debt 台账）；
3. 新 T2 retention（引用计数过期）。

**增强项（可选，将来才做）**：若 basic 不够，**选 graph 优先于 vector**。因为企业中台价值在**关系/时序/演化**（谁改了什么、事实如何冲突演化、agent 间关系），graph（Zep/Graphiti 或 cognee 思想）比 vector 契合；vector 只擅长模糊语义召回（= 我们主动放弃的 A）。增强项**按租户可选、证明必要才上、不进 basic**。

---

## 7. 待决点（需 Owner 拍板，未定不实施）

1. **侧重点压 B（组织资产可治理）** —— 认同？（决定后续所有取舍）
2. **Basic = MD + SQLite 索引（引用计数/标签/关系），不上 vector** —— 认同？
3. **跨 turn 意图交给 T3 聚合、不改 T2 分段** —— 认同？
4. **优先修三个断层（held 重试 / 停滞告警 / retention），先于新功能** —— 认同？
5. **增强项方向（将来做、graph 优先于 vector、按租户可选）** —— 先备着，还是现在排期？

---

## 8. 后续讨论议题（留白，持续叠加）

> 本节记录"还要再讨论的东西"，随讨论补充。（2026-07-02：更完整的断点盘查见 §11.3 —— 那是基于端到端流程视图重新梳理的；本清单是第一轮遗留议题。）

- [ ] 瞬时层"显式可自编辑常驻块"要不要做、怎么做（Letta core memory 对标）。
- [ ] SQLite 索引的具体 schema（标签轴、引用计数、relations、debt 台账）。
- [ ] T3 意图聚合的具体机制（如何让 LLM 判断跨 turn 意图边界、聚合产物形态）。
- [ ] 增强项 graph 的具体边界与租户开关设计。
- [ ] "组织级/跨 agent 知识沉淀"的具体形态（这是 B 的核心差异化，待深挖）。
- [ ] 记忆的权限/可见性模型（跨 agent 共享时的 ACL）。
- [ ] retention/过期策略的完整规则表（对标 Letta 引用次数策略）。
- [ ] 与 Skill（程序记忆）系统的边界与协同。

---

## 9. 从思路到方案：目标架构（2026-07-01，架构味道已对齐）

把 §0.5 的纲落成完整架构。一句话：**按"认知 / 能力"分层、"侧写 / 知识"分平面重组记忆，把被降级的成网零件（§1.5）扶正为一等，给收敛装上反思环，给"自我"建一个活的、一等的家。**

### 9.1 先分清两层：记忆（认知）vs 能力（执行）

一个"会长大的员工"身上有五样东西，性质完全不同，不能按表面形式混：

| # | 员工身上的东西 | 性质 | 归哪 | 组织规律 |
|---|---|---|---|---|
| 1 | 他是谁 | 身份 + 对自己的认识 | soul + **self.md** | 收敛 |
| 2 | 他认识谁 | 对 owner / 同事的画像 | profiles/ | 收敛 |
| 3 | 他懂什么 | 领域专业知识 | knowledge/（wiki 网） | 成网 |
| 4 | 他会做什么 | 能上手的 SOP / 技能 | **Skill + Sub-agent** | 固化（可执行） |
| 5 | 他经历过什么 | 具体事件履历 | episodes（原料） | 向上提炼 |

**记忆（Memory Wiki）= 1、2、3；5 是原料。第 4 样是"能力"，不是记忆。** 对标 LangMem 记忆类型学，一次归位：陈述性（世界"是什么"）→ knowledge 概念网；程序性（"怎么做"）→ Skill（指令集 + 它能召唤的 Sub-agent，是同一 capsule 的两部分 —— 即 Owner 说的"指令集"和"SkillWorker"）；情节（发生过什么）→ episodes；元认知 / 自我（关于我自己）→ self.md（市面唯一对标 = Letta metacognition block / Generative Agents reflection）。

**边界（建议 A，已倾向）**：能力层独立住 `skills/`，**不进** Memory Wiki；记忆里只留"该固化成 Skill 的种子"。连接两层的是一条生长链：

> **经历 → 自我认识 → 固化能力 → 交付执行**
> episode →（self：我发现这类事有个套路 / 我总在这步栽）→（Skill：把套路写成可执行手册）→（Sub-agent：把手册交给专门 worker 跑）

self 是能力的**上游**（先意识到），Skill 是**下游**（固化）。记忆是土壤，Skill/Sub-agent 是长出来、但独立运行的果实。

### 9.2 self vs soul：应然 vs 实然（关键澄清，别再混）

现状 `soul.md` 是混装大杂烩（HR 模板把 Identity / Boundaries / Quality / **How-I-Learn** 焊一起），所以"感觉没区别"。正确判据一刀切开：

- **soul = 我的宪法（应然、被赋予）**：我**应该**是谁、被要求的边界、必须达到的质量标准、服务谁。owner 定，agent 不能改。frozen prefix（缓存锚点）。
- **self = 我的自传 / 体检报告（实然、长出来）**：我**实际**能做什么、实际怎么做、实际常栽哪。经历里自己观察，随时改。

关系：**soul 是标准，self 是朝标准走的进度**。self 里稳定能力可**提名**上升 soul；soul 的要求 self 反复做不到 = 信号（练，或调边界）。

**为什么不能合一**：① 变化速率差一个数量级（soul 季度级冻结 / self 每次经历改）；② prompt cache 边界（合一 → 每次自我更新击穿缓存）。→ **soul 拆纯，把 How-I-Learn / 实际能力 / 失败模式全迁 self**（判断点 2，建议见 §9.7）。

### 9.3 目标结构

```
memory/
  soul.md            身份宪法核（应然，冻结）
  self/self.md       ← 活的自我认知【最大的新东西】
  profiles/          对他人的收敛画像：owner / collaborators / domain
  knowledge/         知识网：<concept>.md，[[链接]] + ## Relations 成网
  episodes/          经历锚点（里程碑，非全部）
—— 以上是"记忆（认知）" ——
skills/              能力（执行）：Skill capsule（指令 + Sub-agent），从记忆长出、独立运行
```

映射：现 `t3/user.md → profiles/`；`worker.md` 的 `constraint`/`blocked_pattern` → `self/`；`capabilities.md` 的 `strategy`/`reference`/`general` → `knowledge/`；`episodes` 保留但只留里程碑（判断点 1）。**目标形态，非"立刻 rename"；迁移按交付纪律一次完整 pass（含 legacy backfill）。**

### 9.4 完整链路：T0 → T2 → T3（每文件四问）

```
T0  每 turn 原始事件（机械真相，已闭合，不动）
 └→ T2  把这段洗成一个 episode（LLM 提炼 + 证据打包）
      └→ 抽养分喂 T3 四区： 关于我→self / 关于他人领域→profiles / 世界知识→knowledge网 / 够重要→episodes锚点
 └→ T3 四区定期「重读—消矛盾—收敛」（反思环）
      └→ 仅身份级极稳定变化 → 才回写 soul.md
```

**T0 —— 机械真相（不重构）**：`t0/sessions/<sid>/segments/<segid>/events.jsonl` + `source.md`，每 turn 一段。产生 = hook 逐事件 append（TURN_STOP 分段，hash 链 + fsync，零智能）；验证 = hash 链 + 可 replay；蒸馏 = seal 后交 T2。

**T2 —— 单段经历洗净打包（episode 成型地）**：`t2/.../segments/<t2id>/{summary,labels,review,manifest}`。产生 = sealed T0 整段喂 LLM（完整视野）→ 三 agent → Platform Gate，事件驱动近实时；验证 = review.md 独立评审 + manifest source_refs 回指 T0；蒸馏 = 抽养分喂 T3 四区，抽完按 retention 归档（不无限留）。

**T3 四区**（职责 / 产生+规则 / 验证 / 蒸馏）：

**① `self/self.md` —— 我对自己的活认识**
- 职责：能力+熟练度 / 方法 / 失败模式（带规避）/ 风格。驱动"越来越强"。
- 产生：每个 T2 episode 收尾，LLM 答"这次暴露了什么能力/方法/失败模式？"→ 对 self.md 的**改写 patch**（merge，非 append）；失败模式必带 episode 引用 + 规避。
- 验证：source_refs 回溯（禁凭空）；Memory Gate 审 patch；**反例下调**（后续 episode 打脸则降级）。
- 蒸馏：dream/heartbeat 定期全量重读，合并重复 / 消矛盾（取新证据）/ 稳定能力上升 / 写长强制收敛。

**② `profiles/owner.md`（+ collaborators / domain）—— 收敛画像**
- 职责：越来越懂 owner / 协作者；领域大局观（细节归 knowledge）。
- 产生：episode 里 owner 偏好 / 反馈 / 纠正 → 更新画像 patch；改写优先，是画像非流水账。
- 验证：source_refs；矛盾留 contradiction 取新证据；`session_feedback` 高权重。
- 蒸馏：同 self，定期重读收敛；写长即失败。

**③ `knowledge/<concept>.md` —— 领域知识网（扶正 wiki lane）**
- 职责：一页一概念（原子），`[[链接]]` + `## Relations` 成网；`Current Claim / Scope / Evidence / Contradictions`。
- 产生：episode 值得沉淀的事实 → curator 判 update vs 新建（反增殖，优先 update）→ patch；新页禁孤儿（至少一条关系边）。
- 验证：低置信 claim 不覆盖（hold）；冲突进 Contradictions 段；source_refs；Memory Gate。
- 蒸馏：被更新时局部收敛；治理孤儿页 / 重复页；relation_graph + PPR 是派生索引（可从 MD 重建）。

**④ `episodes/` —— 里程碑锚点（非全部经历）**
- 职责：少数里程碑（大成功 / 大失败 / 首次 / owner 标记）；self、knowledge 回引它当证据。
- 产生：T2 后过"重要性阈值"才升；多数 T2 抽完养分即归档，不升。
- 验证：重要性判据可解释；被引锚点不悬空。
- 蒸馏：低引用归档 / 高引用保留（引用次数 = retention 依据，对标 Letta）。

**T3 之上 `soul.md` —— 冻结宪法核**：职责 = 拆纯后只留应然（Identity / 服务对象 / 边界 / 质量标准）；产生 = owner/HR 定，dream 仅身份级重大演化才动（frozen-mission gate）；验证 = owner 确认 + Soul Gate；蒸馏 = 几乎不动，self 稳定能力可提名上升（高门槛）。

### 9.5 三个贯穿机制（四问的共性，抽一次）

1. **产生**：全部 **episode（T2）驱动** → LLM 生成 **patch candidate**（改写优先）→ Memory Gate / Platform Gate。无一条机械模板填（AI-Native L1）。
2. **验证 = 残差证据回溯**：每条记忆 `source_refs` 可回 T0 原始事件；Gate 审"真从证据来 + 是否矛盾"；矛盾不删旧的，进 Contradictions 留痕。
3. **蒸馏 = 反思环**：侧写 / self "定期全量重读 → 消矛盾 → 收敛变浓"（现 dream 缺、要补）；知识"局部收敛 + 孤儿/重复治理"；两者都用**引用次数**做 retention。

### 9.6 与 §3–§6 决策一致

- §5（跨 turn 意图 → T3 聚合）不变，意图聚合正是"往两平面提炼"。
- §3（压 B）：两平面 + 显式关系 + 可治理写门 = B 的形态。
- §6（graph 增强）：knowledge 扶正后 `relation_graph` 就是**内建轻量 graph**，多半不必外接图库。
- SQLite（§6）：两平面从 MD 派生同一套索引（概念页关系边增量化、侧写引用计数 / 新鲜度、consolidation-debt 台账）。MD 崩了从 MD 重建。

### 9.7 两个判断点 —— 我的建议

**判断点 1：episodes 只留里程碑 vs 保留全部经历 → 建议「只留里程碑 + 三层兜底 + 追认」。**

这是**假二选一**。"全部经历"已被 **T0（永久机械真相，可 replay）+ T2（全部提炼，retention 归档可召回）** 承载了 —— 什么都没真丢。T3 的 episodes 不必再背"全"这个包袱，它的职责是"值得主动想起的锚点"。所以：
- T3 只 pin 里程碑，保持"活"；归档 ≠ 丢失（T0/T2 兜底）。
- 堵漏判：加**追认**机制 —— 后来发现某段旧经历重要，可从 T2 追认升 T3，解决"当时看不重要、后来才重要"。
- 一句话：**T3 承载"值得主动想起的"，T0/T2 承载"需要时查得到的"。**

**判断点 2：soul.md 拆纯 vs 不拆 → 建议「必须拆，四件套一次 pass 交付，施工顺序 self 先行」。**

**不拆 = 病根三没真修好**：self 建了、soul 里还留 How-I-Learn / 能力 / 边界 → 自我认知两地混装，正是刚批判的病根一重演。所以拆不是可选项。blast radius 用四件套控住，一次交付（**非 MVP 分期**）：
1. 精确边界：soul 只留应然，self 收实然（能力 / 方法 / 失败 / 风格 / How-I-Learn）。
2. 迁移脚本 + legacy backfill：现有 agent soul.md 的实然部分抽出，灌进 self.md 初版。
3. HR 模板同步：新建 agent 时 soul 只生成应然，self 给初始骨架。
4. dream 逻辑改：soul 仅身份级动，实然收敛去 self。

施工顺序：self 读写先跑通（纯新增、不 breaking）→ soul 拆纯迁移随后（breaking）。这是**一次 pass 内的施工步骤，不是分期交付**（区分清楚，守交付纪律）。

### 9.8 下一步

架构味道已对齐。两个判断点若认同上述建议，即进入实施设计：先定 `self/self.md` 字段契约 + 读写 patch 机制（病根三是锋），再排 soul 拆纯迁移四件套，knowledge lane 复活与四文件重组同属一个 pass。收敛反思环（病根二）+ §1.4 三个病症断层（held 重试 / 停滞告警 / retention）配套修。

---

## 10. 实施设计（逐组件，2026-07-02 起）

> §9 是架构总览；本节起逐个组件下沉到字段 / 机制级。按 §9.8 顺序，第一个是 self.md（病根三是锋）。标 **✅已与 owner 确认** 的是拍过板的，其余仍讨论中。

### 10.1 `self/self.md` —— 字段契约（✅已与 owner 确认 2026-07-02）

**形态**：单文件（不是目录），收敛型，可读优先。它是 agent 写给自己的说明书，写长了就是失败——靠蒸馏（§9.5）保持精简，不靠拆分。四个固定 section：能力 / 方法 / 失败模式 / 风格。

**结构示例**：

```markdown
# Self —— 我对自己的认识
<!-- last_reflected: 2026-06-28 -->

## 能力(我能做什么,到什么水平)

### 深度研究 — 熟练
拆解复杂问题、多源检索、交叉验证。DeFi / 协议分析尤其稳。
- 边界:纯学术文献综述弱,倾向抓二手解读
- 证据: t2-a1b2 owner 评"到位" · t2-c3d4
- 锚点: [[ep-web3研报]]
- 确认:2026-06-28

### 数据可视化 — 一般
基础图能画,复杂交互图常返工。
- 证据: t2-e5f6 返工两次

## 方法(我的有效打法)

### 研究任务先列子问题
接到研究先拆 3-5 个子问题再动手,不跑偏。反复验证有效。
- 证据: t2-a1b2 · t2-g7h8

## 失败模式(我常栽哪 + 怎么避)

### 需求含糊时爱自己猜 — 规避中
触发:目标不明确 / 有多种解读。
后果: t2-a1b2 猜错方向,白做两小时。
规避:含糊时先问一个澄清问题,别直接动手。
- 状态:规避中(2026-06-20 起,已成功避开 2 次)

## 风格(我的偏好)
- 先给结论再展开
- 对非技术用户避免术语
```

**字段约定（三条已拍）**：
1. **轻结构，非严格 schema**：散文主体 + 极轻内联元数据（`- 证据 / 边界 / 状态 / 确认`）。严格 schema 违反 AI-Native（机械填表）、LLM 写着啰嗦；纯散文又没法做反例下调与收敛。与现有 T3 `md_store` 格式一脉（散文 + meta 前缀 + XML block）。
2. **证据用来源 ref（`t2-xxx` 指全量 T2 segment，永不悬空）**：详见 §10.9；叙事锚点才用 `[[ep-xxx]]`。每条自我判断可一键回溯 = "残差证据回溯"（§9.5）落到 self 上。
3. **熟练度用档位（熟练 / 一般 / 弱），不用数值**：数值假精确，档位够决策，LLM 自评档位比编数值诚实。

**失败模式生命周期状态（✅已拍要做）**：每条失败模式带状态 **active（在犯）/ 规避中 / 已根除**。这是把"越来越强"从口号变成**可见进度条**的关键一笔——self 不只记"犯过"，而记"正在改、已成功避开 N 次"。同时给蒸馏明确信号：长期 `已根除` 的可归档，`active` 的顶到 prompt 最前提醒自己。代价只是每条多一个状态字段，很轻。

**self 与 soul 的边界 —— constraint 拆分（✅已拍）**：现状 `worker.md` 混了 `blocked_pattern` 和 `constraint`。
- `blocked_pattern` → self 失败模式（无争议）。
- `constraint` 分两种：**被赋予的硬约束**（"不得输出未交叉验证的结论"）→ **soul**（应然、不可自改）；**我实际能力的软边界**（"学术综述我弱"）→ **self 能力边界**（实然、随成长变）。
- 判据（同 §9.2 那把刀）：**别人要求我的 = soul，我观察到自己的 = self。**

**读进 prompt**：self.md 作为 **P0 常驻块**（高优先，始终注入）。收敛保证它精简，整份进 prompt；`active` 失败模式排最前。这同时补上 §4 缺的"瞬时层显式可自编辑常驻块"（Letta core memory 对标）。

**self.md 怎么写**（触发 / patch 形态 / merge-vs-append / gate 四件事）→ §10.2（✅已确认）。

---

### 10.2 `self/self.md` —— 写机制（✅已与 owner 确认 2026-07-02）

四件事收成两组。

**触发 + 路径：搭 T2 便车 + heartbeat 批量，统一管线不另建。**
- **廉价信号标记（每轮，近零成本）**：T2 的 labels agent（本就在读整段打标签）顺手多标一个 `self-signal` 维度——这段有没有暴露关于 agent 自己的东西（owner 反馈 / 返工失败 / 有效打法 / 首次做某类事）。搭便车、不额外跑 LLM，且有整段完整视野，不必另造检测器。
- **昂贵反思（低频批量）**：真正"读 self.md 全文 + 产 patch"在 **heartbeat tick** 批量做，把自上次以来带 self-signal 的 T2 一次喂入。N 个 turn 才反思一次，token 摊薄。
- **统一管线**：self / profiles / knowledge 三区机制同构（抽养分 → LLM 产 patch → Memory Gate → Platform Gate），差异是挂在同一管线上的 policy，**不为 self 另建路径**（另建 = 重复点 = 迟早漂移，即当年 wiki / T3 打架的老坑）。gate 统一：Memory Gate 审证据 / 矛盾，Platform Gate 原子落盘。

**patch 形态 + 同不同判定：operation 信封，判定交 LLM。**
- patch = 若干 operation，每个：`op(add | update | retire) / target(section + 条目) / content(自由散文) / reason / evidence`。operation 层结构化（可审计 / 回滚）、content 层自然语言（AI-Native）。否掉整段改写（diff 不透明、误伤邻条、并发不安全）。
- "新观察算不算老条目" = `op` 填 add 还是 update，**判定权交产 patch 的 LLM**（读 self.md 全文 + 新信号自己判），不用相似度阈值机械卡。

```
op: update
target: 失败模式 / 需求含糊时爱自己猜
content: (含新增"场景·写代码"条件行的整条散文)
reason: ep-201 写代码场景又犯,同一规避适用
evidence: t2-i9j0
```

**母题 + 场景条件（owner 因子类比的落地）：**
- 一条记录 = 一个母题（pattern），母题下挂场景条件（condition）——同一母题在不同场景是"不同 regime 下的变体"，不是两件事。

```markdown
### 需求含糊时爱自己猜 — 规避中
触发:目标不明确 / 多种解读。 规避:先问一个澄清问题。
- 场景·深度研究:高发,规避有效(避开 2 次)t2-a1b2
- 场景·写代码:偶发,规避中 t2-i9j0
- 状态:规避中
```

- 三档处理：**80% 重复（同母题同场景）→ update 加一次证据 confirm（强化不膨胀）；~15% 微调（同母题新场景）→ 母题下加场景条件行；~5% 真新 → add 新母题。**
- 拆分判据（可计算）：默认维持单条；当某场景条件 ① 证据数 ≥ 阈值 且 ② 触发 / 规避与母题主体显著背离（同一规避盖不住），才 promote 成独立母题。同量化"子样本行为与母样本显著背离才单独建模"。判定交 LLM。
- 防膨胀：一个母题挂太多场景条件本身是"该拆或该收敛"的信号（蒸馏处理）。

---

### 10.3 四区塌缩成两套机制（2026-07-02 ✅已确认）

四区不对称的真相：两平面性质不同，四区其实是**两套机制，不是四套**。

- **侧写平面（self + profiles）**：收敛、operation patch、母题 + 场景条件、收敛蒸馏。**profiles 直接复用 self 机制**（§10.1 / 10.2），只是养分从"关于我"换成"关于 owner / 协作者 / 领域"；owner 反馈（`session_feedback`）是高权重信号。
- **知识平面（knowledge + episodes）**：成网、概念页 + 关系边、检索读（§10.4 / 10.5）。

**意义**：补齐四区 = 再啃一套知识平面即可，不是把 self 复制三份。反过来，**强行用 self 的"收敛"去套 knowledge 会误删知识节点**——这正是四区该不对称的深层原因。

**profiles 边界**：`domain.md` vs `knowledge/`——"对领域的总体判断 / 品味"进 domain（收敛，如"Web3 炒作多、要交叉验证"）；"一条条具体概念事实"进 knowledge（成网，如"L2 是什么"）。

**episodes**：核心是"选"不是"写"。重要性判据（工序 1 labels 打分时判，命中才升）：① owner 明确正 / 负反馈 ② 大失败 / 大返工 ③ 首次做成某类任务 ④ 被 self / knowledge patch 引为关键证据。retention 靠引用计数（没人引的老锚点归档）。

### 10.4 knowledge 写机制（知识平面，2026-07-02 ✅已确认）

**复活扶正 `wiki_curator`**（§1.5 被降级零件），非从零。一个概念页 `knowledge/<concept>.md`：

```markdown
---
title: L2 Rollup
status: active
---
## Current Claim   L2 通过把计算移到链下、证明上链来扩容。
## Scope           以太坊扩容;不含 L1 分片。
## Evidence        t2-a1b2 owner 确认 · t2-g7h8
## Contradictions  早期以为 Optimistic 主流,t2-i9j0 显示 ZK 增长更快 → 已更新
## Relations       is_a [[Scaling Solution]] · contrasts_with [[Sharding]]
```

四问：
- **职责**：一页一概念（原子），陈述性知识 + 关系边，成网。
- **产生**：工序 3 抽领域事实养分 → curator 判 update 现有页 vs 新建 → Memory Gate → Platform Gate。**强制成网：新概念页必须建 ≥ 1 条 `## Relations` 边**（禁孤儿）。self 往少了合，knowledge 往关系上连。
- **验证**：低置信 claim 不覆盖 Current Claim（hold）；冲突进 Contradictions 段（不删旧的）；source_refs 回溯。
- **蒸馏（和 self 相反，重点）**：**knowledge 蒸馏 = 织网 + 治理，不是变短。** 知识节点该增就增，蒸馏防三种烂法——**孤儿页**（补关系或归档）、**重复页**（合并）、**巨页**（一页多概念 → 拆）。别拿 self 的收敛压 knowledge。

### 10.5 读侧：侧写常驻 + 知识检索（2026-07-02 ✅已确认）

两平面 → 两种读法：

- **侧写（soul / self / profiles）→ P0 常驻，不检索**：小且永远相关（不管什么任务都要"我是谁 / 我的失败模式 / 我为谁工作"），收敛保证够小、整份进 prompt，放 frozen prefix 附近（cache 友好），self 的 `active` 失败模式顶最前。
- **知识（knowledge / episodes）→ 按 query 检索 top-k**：成网无限增长、不能整份进、只部分相关；用当前 query 做种子跑 **PPR 多跳**，捞相关概念页子网 + 相关里程碑 episode。
- **读取不跑 LLM**：写入时已用 LLM 建好关系，读取就是纯机械"从关系网捞"（§0.5.5"写入智能换检索极简"自洽），保证每次 invoke 快。
- **预算**：侧写占固定额度、知识填剩余；**侧写超预算 = 写侧收敛失败的告警**（触发工序 4），不靠读侧硬截。
- **定位**：非从零——`retriever.py` 已是雏形（高优先 T3 注入 worker P0=0.95 + wiki 走 PPR），要做的是**按两平面把它切干净**（侧写全常驻 / 知识全检索）。
- **治理（后置）**：读侧是跨 agent 共享知识的权限执行点（§8 ACL）；先把单 agent 读侧做对。

### 10.6 实施优先级（2026-07-02）

**读侧 → knowledge → profiles / episodes。** why：先定出口（读）会反哺写侧该写成什么形态、省返工；再补另一半平面（knowledge）；复用的（profiles = self 复用、episodes 只定判据）最后顺手收。

---

### 10.7 成长机制：收敛环 + 生长链上升口（工序 4/5/6，2026-07-02 ✅已确认）

补 §11.3 的 B3 / B4。**成长三工序的共性：都是"提名 + gate"，门槛随影响面递增**——写入(工序 3)agent 自落；收敛改一个文件 → gate + 存档；重固 soul 改身份 → 提名 + owner 批；固化 Skill 加能力 → 提名 + eval。都在 dream/heartbeat 反思时触发，都保留来源链可追溯。

**① 收敛反思环（工序 4，B3）** —— 收敛 ≠ 写入，是两个工序：
- 写入(工序 3) = 增量吸收：operation patch 改局部，否掉整段改写(要精确追踪改了哪条)。
- 收敛(工序 4) = 全局重组：把整个文件当对象重新审视(消重 / 消矛盾 / 提炼 / 删过时)，产出**整份改写**。

具体：
- **触发**：① 脏度(某文件 patch 数 / 字数 / 矛盾标记超阈值——侧写"写长了就是失败"，增长即信号)② 节律兜底(dream 周期扫长期没动的文件)。
- **对象**：侧写整份收敛；知识两级——单概念页收敛 + 网级治理(孤儿 / 重复 / 巨页，§10.4)。
- **输入(L1)**：喂 LLM **完整文件**，不截断(对应"compaction 曾喂 `[-40:]`"反面教训)。
- **输出**：整份改写 + 收敛说明，过 Memory + Platform Gate，**旧版存档可回滚**(复用现有 archive)。
- **安全 = 防误删**：gate 专门审"删除是否合理"；合并条目 source_refs 取并集不丢证据；可回滚兜底。
- **载体**：**dream 从"只碰 soul"扩展到"碰 T3"**——正是病根二缺席的那一环。

**② self → soul（工序 5，B4 上升口 1）** —— 上升不是 agent 自改 soul(soul owner 定、不可自改)，是**提名 + owner 批**：
- 触发：self 某能力长期稳定"熟练" + 高置信 + 大量证据 + 无反例下调 → 提名阈值。
- 动作：dream 生成 soul 提名(附证据)。批准：Soul Gate + **owner 确认**(身份级变更 owner-in-the-loop)。
- 吻合 §9.2"soul 是标准、self 是进度"：能力练成招牌 → 从"我能做"升为"我是谁"，但 owner 点头。低频、重。

**③ self → Skill（工序 6，B4 上升口 2）** —— 生长链核心下游，本质"软 → 硬"转写 + 交接：
- 触发：self 某"方法"反复确认有效(多证据无反例)，或某失败模式规避反复成功(状态 → 已根除)。
- 动作：LLM 把叙述性自我认知转写成可执行 Skill 候选(self"我先列子问题"软 → Skill"深度研究 SOP:1…"硬)。
- **交接非直造**：记忆只产 Skill 候选，交能力层 Skill 系统既有 eval / promotion(Skill 本从 eval-backed candidate 生长)。两层边界清晰。
- 固化后 self 那条**不删，标注 `已固化 → [[skill-深度研究]]`**：自我认知仍成立、只是有了可执行版本。self ↔ Skill 双向链 = "留种子、果实在 skills/、种子指向果实"落地。

**闭环**：这三工序补完，病根二(收敛)+ 病根三出口(变强的能力真固化出去)闭环。

---

### 10.8 直接输入通道：explicit + feedback（2026-07-02 ✅已确认）

补回顾发现的缺口：写入此前全是"自然提炼"慢通道（经历 → T2 → 工序 3 消化），但两类用户直接信号绕过经历、要立即生效——它们**不新增区，是汇入四区的两条快入口**。

**核心区分**：explicit = 无主的新内容（本身就是一条记忆，要下沉四区）；feedback = 有主的评价（针对某 decision，改造那段经历提炼出的记忆）。路径不同，别硬统一。

**explicit（用户"记住 X"）—— 快注入 + 工序 3 分流 + 退役：**
- 立即写 `memory/explicit/` overlay + 当轮 P0 激活（用户明确要记的，本轮生效，不等 heartbeat）。
- 工序 3 消化时 LLM 分流进四区（"喜欢简洁"→ profiles /"L2 是 X"→ knowledge /"我这任务老错"→ self）。
- 分流后 overlay 条目标 `absorbed → [[目标]]` 退役，**与四区写入同一次工序 3 原子交接**——防 overlay 与四区双重注入。
- nuance：时效性条目（"会议室改 302"）不下沉，留 overlay 待 retention 过期；分流与否 LLM 判。

**feedback（用户"错了 / 做得好"）—— 挂 decision + 工序 3 当极性信号改造自然产物：**
- 不独立成条，link 到 `decision/<id>`；负反馈当轮即标该结论"已被 owner 否定"，防 agent 继续用错。
- 工序 3 作为高权重信号：正反馈 → 强化（self 能力 confirm↑、方法标"有效"，逼近 Skill 固化阈值）；负反馈 → ① self 触发 / 更新失败模式 ② knowledge 进 Contradictions + 修正 Current Claim。
- 直接喂 self "反例下调"（§10.1）——负反馈是最强打脸信号。

**三通道汇合（统一 sink 四区，分级入口）：**
```
自然     经历 → T2(中性提炼) ─────────────┐
explicit 用户注入内容 → overlay(快激活) → 分流 ─┼─→ 四区
feedback 用户评价经历 → 挂 decision(快生效) → 加极性 ─┘
```
自然 = agent 自推（中权重、要证据验证）；explicit = 用户给内容（高可信、快激活 + 分流）；feedback = 用户给极性（高权重、改造产物）。不新增区，保住三种信号可信度差异。

**治理**：explicit / feedback 走写 gate（现状已强制 sensitivity / privacy，PL4 拒）；组织层"谁能注入"ACL 后置（Goal 2），当前单 agent owner / creator 能。

---

### 10.9 来源 ref 体系（横切所有区，2026-07-02 ✅已确认）

解 §12.3 主 + 次断点。**"来源"是两种性质相反的东西，必须分开：**

- **证据链（evidence）—— 指不可变历史快照**：一条记忆"凭什么这么说"，指 T2 segment / explicit / decision（发生即钉死、永远在）。**必须有**，验证用。
- **叙事锚点 + 活记忆引用 —— 指会变的东西**：关联的里程碑 episode、knowledge Relations 边。**可选**，导航用。

**id 规范**（MD 写短 id，SQLite 派生解析）：`t2-<id>` = T2 segment（证据主力，全量）· `ep-<id>` = episodes 里程碑（只作叙事锚点）· `ex-<id>` = explicit · `fb-<id>` = feedback/decision · `[[k:概念]]` = 记忆间活引用。

**证据只指不可变源（解主断点，一条硬原则）**：证据只能指 `t2-` / `ex-` / `fb-`，**绝不指 `ep-` 或活记忆条目**。证据的作用是回溯到钉死的事实——之前 self 用 `[[ep-123]]` 当证据、而 ep-123 多数没升 episodes 才悬空；改指全量 T2 就**永不悬空**，episodes 从此只当叙事锚点。一条记忆两栏：

```markdown
- 证据: t2-a1b2 · t2-c3d4 · fb-d45   ← 必须,指不可变源,永不悬空
- 锚点: [[ep-web3研报]]                ← 可选,只在关联里程碑时有
```

**活引用用稳定 id + tombstone（解次断点）**：knowledge Relations、self→soul 提名、self→Skill 固化这些活引用，指条目稳定 id（`self:fm-爱猜` / `k:l2-rollup`）不指内容；收敛（工序 4）合并两条时保留主 id、次 id 留 tombstone → 主 id，引用自动跟随；SQLite 反向索引（"谁引用了我"）让收敛能一并更新。

**SQLite 顺带闭合 B5 retention**：MD 是真相（正向 ref），SQLite 派生（反向索引 + id 解析，可从 MD 重建）。**反向索引的 count 就是引用计数**——一段 T2 / 一个 episode 被多少条记忆引用，SQLite 一数就有，正是 retention 要的（没人引的老锚点归档）。**一个设计解主断点 + 次断点 + B5。**

---

## 11. 端到端视图：完整流程 + 产物清单 + 断点（2026-07-02）

> §9 / §10 是分层设计；本节把整条线串成一张流水线，并基于它盘断点。回答 owner 两问：改完后完整流程几道工序、产出哪些文件。

### 11.1 完整流程：一段经历从发生到沉淀，七道工序

- **工序 0 — 经历发生 → T0 写入**（实时 · 机械 · 零 LLM）：每事件即时 append `events.jsonl` + `source.md` 投影；`TURN_STOP` 分段，一段 = 一个 user turn。
- **工序 1 — 洗净打包 → T2**（回合级近实时 · 1 次 LLM 三 agent）：sealed T0 段整段喂 LLM 产 summary / labels / review；labels agent 顺带 ① 标 `self-signal` ② 四区养分归类 ③ 重要性打分。
- **工序 2 — 分流标记**（搭工序 1 便车 · 近零成本）：决定这段升不升 `episodes/` 锚点、给四区各抽什么养分，产出 intake 候选进 heartbeat 待办。
- **工序 3 — 批量消化 → T3**（heartbeat 低频 · 批量 LLM）：读累积养分，对四区各产 operation patch → Memory Gate（审证据 / 矛盾）→ Platform Gate（原子落盘）。
- **工序 4 — 收敛 / 反思环**（heartbeat / dream 定期 · 全量重读 LLM）：定期全量重读某 T3 文件，合并重复 / 消矛盾 / 删过时 / 收敛变浓；引用计数做 retention。**这是病根二的正解。**
- **工序 5 — 身份重固 → soul**（dream 极低频 · 高门槛）：self 极稳定能力提名上升 soul，过 Soul Gate；只在身份级重大演化才动。
- **工序 6 — 能力固化 → Skill**（事件触发 · 交接能力层）：self 反复确认的方法 / 规避提名固化成 Skill（指令 + Sub-agent），交接后独立走 eval / promotion。**生长链下游，离开记忆管线。**

小结：工序 0-3 = 记录 → 提炼（晴天路径基本已有）；工序 4-6 = 成长（病根二 / 三的正解，本次重构重心）。

### 11.2 完整产物清单（文件树）

```
memory/
  soul.md                                     身份宪法核(应然,冻结)
  self/self.md                                ★自我认知(能力/方法/失败模式/风格)
  profiles/{owner,collaborators,domain}.md    ★对他人的收敛画像
  knowledge/<concept>.md                      ★知识网(原子概念页,[[链接]]+## Relations)
  episodes/<episode>.md                       ★里程碑经历锚点
  t0/sessions/<sid>/segments/<segid>/
    events.jsonl · source.md                  机械真相 + 投影(不变)
  t2/sessions/<sid>/segments/<t2id>/
    summary.md · labels.md · review.md · manifest.json   单段 episode 打包(不变)
  indexes/
    wiki_map.md                               生成的导航图
    index.sqlite                              派生索引(关系边/引用计数/tag/debt台账,可从MD重建)
  control/                                    sidecar(consolidation-debt / retention 台账)
—— 以下是能力层,不属记忆 ——
skills/<skill>/                               Skill capsule(instructions/refs/templates/scripts/evals/subagent)
```

★ = 本次重构新增 / 变动（由旧 `t3/{episodes,user,worker,capabilities}.md` 四文件重组而来）。

### 11.3 断点盘查（基于上面流程，诚实标未完成）

> 状态（2026-07-02）：**已闭合** A1 / A2 / B3 / B4 / B6 + 回顾新增"直接输入通道（explicit / feedback）"→ 见 §10.3-10.8。**仍开** C7 迁移方案 · C8 SQLite schema（施工级）。§12 审查断点（证据 ref / 收敛引用 / B5 retention）已由 §10.9 来源 ref 体系一并解决。

**A. 结构性大断点（下一步该优先讨论）：**
1. **四区不对称 —— self 深，knowledge / profiles / episodes 浅**。self 有字段（§10.1）+ 机制（§10.2）；另三区只有 §9.4 粗描述。尤其 **knowledge 写机制**（episode → 概念页：怎么判改哪页、怎么建关系边、原子粒度）是"两平面"的另一半，不能只做一半。
2. **读侧几乎空白**。§9-§11 全是写侧（T0 → T3）。但"记忆怎么回到 prompt"——四区各自何时进、self P0 常驻而 knowledge / profiles 按什么激活、token 预算怎么分——只有零星提及。**写得再好、读不进 prompt = 没用。** 目前最大结构性缺口。

**B. 机制待细化（方向已定，未成文）：**
3. 收敛反思环（病根二）：频率 / 触发 / LLM 看什么 / 产出什么，§9.5 只一句。
4. 生长链两个上升口：self → soul 提名、self → Skill 固化，机制空白（§9.1 只画了链）。
5. retention / 引用计数：谁数、怎么统一实现、完整规则表（§8 已列）。
6. episodes 重要性判据：什么算里程碑、阈值（判断点 1 定了"只留里程碑"，判据未定）。

**C. 待落地（已决方向，施工级）：**
7. 迁移方案：旧四文件 → 两平面 + self 的完整 migration（§9.3 / §9.7 有原则，无步骤）。
8. SQLite 索引 schema（§8 已列）。

### 11.4 顺畅度：两轮叠加待缝合

文档是两轮层积：§0-§8（第一轮，自下而上：对标 / 压 B / 修断层）+ §0.5·§1.5·§9-§11（第二轮，自上而下：两平面 / self）。结论一致，但三处"待议清单"各自为政、需统一：§7（5 待决，第一轮）、§9.7（2 判断点，已确认）、§8（8 议题）。建议：§9.7 已确认的移出待决；§7 逐条标状态；§8 收编 §11.3 为统一入口。

---

## 12. 闭环审查（2026-07-02）

三问审查（出处 / 核心逻辑 / 耦合）+ owner 重点关注的 T3 文件清点。

### 12.1 T3 到底几个文件（三级概念：层 → 平面 → 文件，先理清命名）

一个概念断点先说清：**T3 是"层"，内含两平面，平面装文件**——三级，全文别混用。
- **T3 层** = 记忆区（`soul.md` 在其上，不属 T3）。
- **侧写平面（收敛）** = self + profiles。
- **知识平面（成网）** = knowledge + episodes。

| 层 / 平面 | 文件 | 数量 | 旧 T3 来源 |
|---|---|---|---|
| soul（T3 之上） | `soul.md` | 1（拆纯） | 原 soul + 旧 `worker.md` 硬约束 |
| 侧写平面 | `self/self.md` | 1 | 旧 `worker.md`（blocked_pattern / 能力）+ `capabilities.md` 能力自评 |
| 侧写平面 | `profiles/{owner,collaborators,domain}.md` | 3（固定） | 旧 `user.md` |
| 知识平面 | `knowledge/<concept>.md` | N（随经历长） | 旧 `capabilities.md`（strategy / reference / general） |
| 知识平面 | `episodes/<episode>.md` | M（里程碑） | 旧 `episodes.md`（多数留 T2 归档） |
| 快通道（非区） | `explicit/` overlay | 暂存 | 现状 explicit |

固定收敛文件 = soul + self + profiles×3 = **5 个**；成网 / 锚点 = knowledge(N) + episodes(M) 不定。旧 4 个扁平文件 → 新两平面。

### 12.2 三问结论

1. **出处（都有明确 writer）✓**：soul ← owner/HR + dream 提名；self / profiles ← 工序 3（T2 养分 + explicit 分流 + feedback 极性）；knowledge ← curator；episodes ← 工序 2 重要性升；explicit ← save_memory。
2. **核心逻辑 ✓**：两平面（侧写收敛 / 知识成网）；AI-Native（patch 由 LLM、读不跑 LLM、收敛不截断）；MD-first（SQLite 派生可重建）；全程 Memory/Platform/Soul Gate + Skill eval。
3. **耦合（密，但有缝）**：联系网 = self↔episodes（证据）· knowledge↔episodes（证据）· knowledge↔knowledge（Relations 成网）· self↔Skill（固化双向链）· self↔soul（提名）· explicit/feedback↔四区（分流/改造）· profiles≈self（同机制）。缝见 §12.3。

### 12.3 审查发现的新断点

> 更新（2026-07-02）：**主断点 + 次断点已由 §10.9 来源 ref 体系解决**（证据指全量 T2 永不悬空；活引用稳定 id + tombstone；反向索引 count 顺带闭合 B5 retention）。概念断点（命名）见 §12.1。以下保留问题描述存档。

**主断点 —— 证据引用体系不统一。** self / knowledge 用 `[[ep-xxx]]` 引证据，但：
- episodes 只留里程碑（多数经历是 T2、不升 episodes）。self"我擅长研究，证据 [[ep-123]]"的 ep-123 大概率是普通 T2，不是 episodes 锚点。
- 于是两种引用语义混用：**证据回溯**（该指全量 T2 segment，都在）vs **叙事锚点**（指 episodes 里程碑，少数）。现在都写 `[[ep-xxx]]` 不分。
- 加上 explicit（ex-id）、feedback（decision-id）也是记忆来源。
- → 需统一**来源 ref 体系**：一条记忆的证据能指 T2 segment / episodes / explicit / decision，统一 id 规范，且区分"证据链 vs 叙事锚点"两种引用。

**次断点 —— 收敛跨文件引用完整性。** self 收敛（工序 4）合并 / 删条目时，soul（提名来源）、Skill（固化来源）对该条目的反向引用可能悬空。§10.7 只说了文件内 source_refs 取并集，跨文件反向引用没维护 → 收敛要更新外部引用或留 tombstone。

**概念断点 —— T3 / 四区 / 两平面三级命名**（§12.1 已理清，全文用词待统一）。

---

## 附：主要证据来源

**现状（代码，当前 branch）**：`memory/t0/ledger.py`、`runtime/hooks_setup.py`、`memory/t2/segment_package.py`、`services/heartbeat.py`、`services/auto_dream.py`、`runtime/invoker.py`、`services/web_chat_runtime.py`、`runtime/ccplus_contracts.py`、`memory/memory_service.py`。

**市面对标（实时检索）**：
- LOCOMO：`snap-research.github.io/locomo` + `arxiv.org/abs/2402.17753`（10 对话/27.2 sessions/1540 题；~300 turns/~9K tokens 为论文原始计数）。
- Zep–Mem0 benchmark 争议：`github.com/getzep/zep-papers/issues/5` + Zep blog "lies, damn lies, and statistics"。
- Letta sleep-time / consolidation：`forum.letta.com/t/sleeptime-agents-for-memory-consolidation-best-practices-guide/154`。
- MemGPT：arXiv（memory tiers）。Cognee：`github.com/topoteretes/cognee`。Mem0：`github.com/mem0ai/mem0` + 论文。A-Mem：arXiv "A-Mem: Agentic Memory for LLM Agents"。Generative Agents：arXiv "Generative Agents: Interactive Simulacra of Human Behavior"。

> 未验证、不采用：LOCOMO "588.2 turns / 17,390 tokens"、"human ≈88 vs GPT-4 ≈32" 等来自不可达来源的具体数，勿引用。
