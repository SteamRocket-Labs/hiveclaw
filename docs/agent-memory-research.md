# Agent Memory 研究报告 — 理论基础与实践参照（为 MD-first 记忆引擎规格化服务）

> 日期：2026-06-04 · 服务对象：`docs/knowledge-container-boundaries.md` 收敛为"环节全显式的完整规格"
> 方法：deep-research workflow（部分成功，抢救 59 条一手论断）+ 7 路 agent 克隆源码深读 + 主 agent 亲读 Tencent/basic-memory 源码
> 原则：所有结论带引用（arXiv 链接 / GitHub 文件路径）。脑科学是检验"蒸馏"设计的第一性原理，但**本报告对"蒸馏=脑科学"的假设给出证伪性结论，不做护教**。

---

## 0. 一个必须先说的诚实结论（直接挑战原始设计假设）

原始假设是「所有蒸馏过程最终都基于脑科学的记忆巩固原理」。**4 篇架构论文的对照证伪了这个假设的强版本**：

| 论文 | 脑科学声称 | 类比性质 | 是否"可工程化真判据" |
|---|---|---|---|
| **HippoRAG** ([2405.14831](https://arxiv.org/abs/2405.14831)) | 最强（标题即 neurobiological） | **功能同构**：neocortex=LLM / PHR=检索编码器 / hippocampus=KG+PPR，编码与检索双路径逐段对应、各有可证伪的工程选择 | ✅ **唯一真判据** |
| **Zep** ([2501.13956](https://arxiv.org/abs/2501.13956)) | 中（episodic/semantic 二分） | **认知心理学借用**：指导"原始 episode 层 + 派生 semantic 层"双存储，有架构后果但无神经回路对应 | 🟡 半真 |
| **MemGPT** ([2310.08560](https://arxiv.org/abs/2310.08560)) | 零（自称 OS） | RAM/disk/page **纯 OS 隐喻**，换个名字机制不变 | ❌ 松散比喻 |
| **MemOS** ([2507.03724](https://arxiv.org/abs/2507.03724)) | 零（主动撇清，自归 OS Stage 4） | CPU/文件系统/系统调用类比，**明示拒绝脑科学标签** | ❌ 松散比喻（但 OS 工程扎实） |

**真假分水岭 = 类比是否产生"可独立证伪的工程选择"。** HippoRAG 的「PPR = 海马体 pattern completion」不是给 PageRank 起生物名，而是从神经理论**推导出**"用一次图扩散替代多跳迭代检索"，并在 multi-hop benchmark 上被验证（+20.9% R@5，便宜 10-30×）。类比→设计→可测后果三段闭环。

**对 Hive 的直接含义**：Hive 现在的 T0→T2→T3→soul 本质是 **MemOS「明文态（plaintext memory）」内部的多级压缩**，全程没碰激活态/参数态——**这是诚实的工程，与脑科学无关，不必硬贴神经标签**。脑科学真正能给 Hive 的，不是金字塔层的生物命名，而是 §A 里**少数几条产生可工程化判据的机制**（主动遗忘的可逆性、CLS 交错重放、巩固周期），以及一条可选的硬路线（HippoRAG 式 KG+PPR 检索）。

---

## A) 脑科学记忆原理地图 — 哪些机制能给出可工程化判据

> 来源：CLS 原文 McClelland/McNaughton/O'Reilly 1995（[wixtedlab PDF](http://wixtedlab.ucsd.edu/publications/Psych%20218/McClellandMcNaughtonOReilly95.pdf)）；遗忘综述 Nature Rev Neurosci [s41583-021-00548-3](https://www.nature.com/articles/s41583-021-00548-3)；主动遗忘 Cell/Neuron [S0896-6273(17)30498-1](https://www.cell.com/neuron/fulltext/S0896-6273(17)30498-1)；遗忘可逆 eLife [92860](https://elifesciences.org/articles/92860)。

| 脑机制 | 核心结论 | 对 Hive 的可工程化判据 | Hive 现状映射 |
|---|---|---|---|
| **CLS 互补学习系统** | 海马体快速学单条经验、新皮层慢速整合；**交错重放（interleaved replay）**把新旧记忆混合回放，防止新知识灾难性覆盖旧知识 | 巩固（heartbeat/dream）必须**新旧混合**输入 LLM，不能只喂新增 delta——否则等于灾难性遗忘 | ⚠️ heartbeat 增量游标只看新 T2，缺"新旧交错"——**可改进点** |
| **系统巩固 + 海马体重放** | 记忆从海马依赖逐步转为新皮层独立；巩固发生在离线期（睡眠重放） | 巩固应是**周期性离线批处理**（非每条实时），且"重要的强化、不重要的修剪" | ✅ full dream（24h + activity gate）是离线身份巩固周期，soft dream 是 T3 缓压维护 |
| **Tulving 情景 vs 语义** | episodic（带时空的具体事件）vs semantic（抽象去情境的知识）是不同系统 | 可指导"T0 ledger/原始事件层" vs "knowledge T3/抽象知识层"双存储 | 🟡 **半真借用**（Zep 级）——有架构后果，但别当"脑科学证据"卖 |
| **遗忘 = 主动神经可塑性** | 遗忘不是衰减失败，是**有专门分子机器的主动过程**，默认倾向擦除、与巩固竞争 | ⑧退役应是**默认开启的侵蚀**，与"升层巩固"竞争——不是"没强化就留着" | 🔴 Hive ⑧退役几乎不发生=违背此原理 |
| **遗忘 = accessible→inaccessible 而非擦除（可逆）** | 遗忘只是让记忆"检索不到"，痕迹仍在，可被重新激活恢复（光遗传学实证） | **退役 = de-index/降级出检索注入，而非物删源文件**——MD 留档、只移出活跃召回 | ✅ 与 MD-first 完美契合；Graphiti 的 `expired_at` 是此机制的工程写实 |
| **干扰 vs 衰减（两类遗忘）** | 干扰（新信息挤掉旧）与慢性衰减（时间）是不同机制 | 退役判据应**分两条**：干扰型（新事实矛盾旧→dedup/冲突消解）+ 衰减型（长期零召回→时间归档） | 🔴 Hive 两条都缺判定契约 |
| **遗忘是适应性的（提升检索精度）** | 清理无关记忆**提高检索精度、减少无效坚持**——遗忘是 feature 不是 data loss | **防增殖的生物学依据**：激进退役低价值记忆改善召回质量，不是损失 | 直接支撑「容量 cap 强制蒸馏」（Tencent 模式） |
| **遗忘率应随环境失配调节** | 在已不相关环境中获得的记忆是优先遗忘对象 | owner/company/task 域已漂移的记忆 = 优先退役候选 | 接 Hive principal_context（owner/company 漂移检测） |

**脑科学给不出判据、必须工程自定的（survey [2603.07670](https://arxiv.org/html/2603.07670v1) 明列为 open problem）**：
1. **importance 怎么估**——无未来视角时无法知道一条记忆将来有没有用（Generative Agents 用 LLM 打 poignancy 1-10 是工程近似，非脑科学）；
2. **何时该巩固**——"检测到需要巩固"没有生物学阈值，各家用 token 压力 / 计数 / 时间间隔（全是工程拍的数）；
3. **安全关键记忆怎么保活**——生物遗忘默认擦除，但工程上 PL4/审计记忆必须永不丢，这是治理需求、脑科学反而有害。

> **结论**：脑科学能背书的是 **③④⑤⑦⑧ 的"形状"**（巩固周期、交错重放、退役=可逆 de-index、干扰/衰减两条退役线、防增殖的正当性），但**所有具体阈值（升层分数、容量上限、巩固间隔、importance 评分）都是工程自定，无脑科学判据**。诚实的说法是「**Hive 的蒸馏在结构上受巩固/遗忘原理启发，但判据是工程的**」。

---

## B) 架构论文机制对照表

| 论文 | 分层架构 | 蒸馏/巩固机制 | 触发时机 | 遗忘/退役 | 检索/注入 | 脑科学映射 |
|---|---|---|---|---|---|---|
| **MemGPT** | Main context(RAM: 指令/working/FIFO+递归摘要) ↔ External(recall 全量/archival) | 70%警告→LLM 主动转存；100%→驱逐50%+无损递归摘要 | **事件驱动**（token 越界，仿 page-fault） | 无真遗忘（只在 RAM↔disk 移动，recall 永久） | function calling 自管理（search page 回主上下文）；heartbeat 链式 | ❌ 纯 OS |
| **Generative Agents** ([2304.03442](https://ar5iv.labs.arxiv.org/html/2304.03442)) | memory stream（带 recency/importance/relevance）→ reflection 树 | reflection 两步：抽 3 个最显著问题→推 5 条洞察（带 because-of 引用） | **事件驱动**（累计 importance > 150，约每天 2-3 次） | recency 指数衰减（软降权，不删） | score = recency + importance + relevance（三权重各=1） | 🟡 importance=poignancy 1-10 是工程近似 |
| **A-MEM** ([2502.12110](https://arxiv.org/abs/2502.12110)) | 原子 note（结构化属性：context/keywords/tags） | Zettelkasten：新记忆分析历史、自动建链；memory evolution 更新旧 note | 事件驱动（每次写入） | memory evolution 精炼旧 note（非删） | 动态索引 + 链网检索 | — |
| **Reflexion** ([2303.11366](https://arxiv.org/abs/2303.11366)) | 短期（原始轨迹）vs 长期（自反思文本） | verbal self-reflection 存 episodic buffer，下次试验改进 | 事件驱动（每次 trial 后） | Ω 容量界（1-3，按 context budget 裁） | 长期反思注入下一轮 | — |
| **Voyager** ([2305.16291](https://arxiv.org/abs/2305.16291)) | skill library（可执行代码，非文本） | 技能经**自验证 gate** 通过后才入库 | 事件驱动（任务成功后） | 技能不退役（可组合复用） | 按 query embedding 检索可执行技能 | — |
| **HippoRAG** | neocortex(LLM)/PHR(检索器)/hippocampus(KG+PPR) | OpenIE 抽三元组建无 schema KG + 同义边 | **离线一次性建索引**（=编码巩固） | 无显式遗忘；node specificity（局部 IDF）软降权 | **PPR 单步多跳**（partial cue→pattern completion） | ✅ 功能同构（真判据） |
| **Zep/Graphiti** | episodic 子图/semantic 实体子图/community 子图 | LLM 抽实体+fact triple；label propagation 建社区 | **事件驱动 ingestion + 社区周期刷新**（双模） | **bi-temporal 失效**（旧边 `invalid_at`=新边 `valid_at`，不删） | 三路混合检索（BM25+向量+图）+ 上下文压缩 115k→1.6k token | 🟡 episodic/semantic 二分 |
| **MemOS** | 三态：plaintext / activation(KV-cache) / parameter(LoRA) | 三态互转：明文→激活（省 prefill）；稳定知识→参数（蒸馏）；冷参数→明文 | **事件+策略混合**（语义触发调度 + TTL 周期淘汰） | MemLifecycle 五态机 Generated→Activated→Merged→Archived→Frozen + TTL/decay | MemReader(NL→MemoryCall) + 多视角混合检索 | ❌ 纯 OS（自归 Stage 4 治理） |
| **Survey** ([2603.07670](https://arxiv.org/html/2603.07670v1)) | 三维分类法：temporal scope(working/episodic/semantic/procedural) × substrate(text/vector/structured/executable) × control(heuristic/prompted/learned) | — | — | — | — | 提醒：神经巩固只是 suggestive model，非工程判据来源 |

**对 Hive 最直接可借的两条架构判据**：
1. **Generative Agents 的三因子检索**（recency+importance+relevance）正是 Hive `retriever.py` 该有的打分骨架——Hive 当前缺 recency 衰减与 importance 显式分。
2. **MemOS 的 MemCube 元数据头**（Descriptive + Governance{access/lifespan/compliance} + Behavioral{access pattern/version chain}）是 Hive 记忆"携带生命周期/证据元数据"要求的现成 schema 蓝本。

---

## C) 开源项目八环节对照（含蒸馏提示词骨架）

> 八环节 = 写入①→蒸馏②→归档③→索引④→检索⑤→主动查⑥→维护⑦→退役⑧（Hive `knowledge-container-boundaries.md §9`）

### C.1 八家 × 八环节速查

| 项目 | 事实源 | ①写入 | ②蒸馏 | ③归档分类 | ④索引 | ⑤⑥检索 | ⑦维护 | ⑧退役 |
|---|---|---|---|---|---|---|---|---|
| **Tencent** ⭐ | **MD（L2/L3）+JSONL（L0/L1）** | L0 recorder | L1 抽取+情境切分 LLM | persona/episodic/instruction 3 类 + scene 文件 | **scene_index.json→navigation 注入 persona.md（heat 排序）** | heat 排序+渐进披露 read_file | L1 dedup(store/skip/update/merge)+L2 容量 cap merge | **soft-delete `[DELETED]` + 容量 cap 强制合并** |
| **agentmemory** | KV（MD 仅导出） | hooks observe | 5 段：compress→summary→semantic→reflect→graph | type 枚举+concepts 自由 | BM25+向量+图 RRF | hybrid+reranker | superseded 软退+dedup | **auto-forget（TTL/矛盾/低价值）+ retention Ebbinghaus 衰减驱逐** |
| **Mem0** | 向量+SQLite history | add() | fact extraction LLM | 20 类软枚举（可新建） | 向量+实体链接 | top_k 向量 | **ADD/UPDATE/DELETE/NOOP 控制器**（经典）/ hash 去重（V3） | 经典：LLM 判矛盾删；V3：append-only 软链接 |
| **Graphiti** | 图库 | add_episode | LLM 抽实体+fact | 自定义 ontology（Pydantic） | 三层子图 | 混合检索+时间过滤 | LLM 判矛盾+Python 判时间 | **bi-temporal 失效非删（de-index）** |
| **Letta** | DB block | agent 自调工具 | sleep-time agent rethink | block label（自由）+tags | 上下文内联 + archival 向量 | core 全量注入 + archival 按需 search | sleep-time 重写+唯一性守卫 | 主动改写（`replace ""`）；archival 永久 |
| **LangMem** | store-agnostic（JSON） | manage_memory 工具 | `_MEMORY_INSTRUCTIONS`（置信度/SNR） | semantic/episodic/procedural 三类 | namespace 目录+向量 | hot-path search / background 注入 | search→consolidate→patch/RemoveDoc | RemoveDoc 显式删（默认关） |
| **claude-mem** | SQLite（MD 可选渲染） | hook 抓 session | compress→observation LLM | type×concept（双轴，**驱动告警**） | **index + get_observations 三步取详情** | FTS 主路+语义注入 opt-in | timeline 去重 | 时间归档 |
| **basic-memory** ⭐ | **纯 MD（SQLite 派生）** | 人/agent 写 .md | 无 LLM（纯结构化） | `[category]` 观察 + frontmatter | **[[wikilink]] 图 + SQLite 镜像 + LinkResolver** | 全文+图遍历 | sync 增量 | 删文件（MD 即源） |

⭐ = MD-first，与 Hive 同范式，优先借鉴。

### C.2 关键蒸馏提示词骨架（可直接对照 Hive 改写）

**Tencent L1 抽取**（`src/core/prompts/l1-extraction.ts:16`）— 单次 LLM 调用同时做情境切分+提取，**只 3 类**：
- `persona`（稳定属性/偏好，priority 80-100 健康禁忌 / 50-70 一般 / <50 丢）
- `episodic`（客观事件，"用户在[绝对时间]于[地点][做了某事]"，绝不含纯主观感受）
- `instruction`（长期行为规则，priority **-1=绝对死命令** / 90-100 核心 / <70 临时丢）
- 三原则：**宁缺毋滥 / 独立完整（跳出对话仍成立）/ 归纳合并**

**Tencent L1 去重**（`l1-dedup.ts:15`）— 比 Mem0 更强的冲突消解：批量统一候选池一次过，4 动作 `store/skip/update/merge` + **跨 type 合并** + **多对多替换（target_ids 数组）** + `merged_timestamps` 取时间戳并集（保留时间线）+ 合并后 priority 酌情提升。

**Tencent L2 场景巩固**（`scene-extraction.ts:45`，"Memory Consolidation Architect"）— 防增殖范本：
- 角色="数字第二大脑"/人类学家，**L1 碎片→连贯叙事文档（禁止列表追加）**
- **容量硬上限 + 三色预警**：红(≥max)必须先 MERGE / 橙(=max-1)只 UPDATE / 黄优先合并
- **默认 UPDATE 非 CREATE**；CREATE 须先 read 2 个最相似场景确认无法融入；每批最多新建 1 个
- **heat 三态**：新建=1 / 更新=旧+1 / 合并=sum+1 → 驱动导航排序 + 合并牺牲者（heat 最低先合）
- **`[DELETED]` 软删**，明令禁止 `[ARCHIVE]`/`[CONSOLIDATED]` 替代触发清理（**反改名逃逸**）
- 文件模板：META + 核心特征(连贯段<100字) + 偏好(可列表) + **隐性信号(推断)** + **核心叙事(Trigger→Action→Result <400字)** + **演变轨迹(冲突不覆盖记轨迹+记忆ID引用)** + 待确认/矛盾点

**Tencent L3 Persona 生成**（`persona-generation.ts:33`）— =Hive dream→soul：
- **用 write/edit 文件工具写 persona.md（MD-first）**，2000 字硬上限
- **四层深度扫描**：基础锚点→兴趣图谱(区分活跃/被动/休眠)→交互协议(怎么说话)→认知内核(决策逻辑)
- **"叙事连贯性 / 寻找贯穿线 / 禁止 bullet-point 罗列"**（正是 Deep Research RC15"要分析师不要信息聚合器"的现成解法）
- 反幻觉：**禁止用非场景来源信息**（不从 workspace 目录/文件路径/系统元数据提取用户信息=治理边界）
- 增量决策：强化/补充/修正/重构/不改

**Mem0 记忆控制器**（`mem0/configs/prompts.py:176`，经典 ADD/UPDATE/DELETE/NOOP）— Hive ⑦⑧最缺的"逐条裁决"契约：把检索到的现存记忆**全量喂 LLM**，对每条新事实输出 `{id, event∈ADD/UPDATE/DELETE/NONE, text, old_memory}`，DELETE 触发=新事实矛盾旧记忆。⚠️ **反面教材**：Mem0 生产路径 V3 已把这步降级成 hash 去重 + spaCy 实体（机械上位主路径）——Hive 该抄它的**契约形态**，**别抄它把智能步骤机械化**。

**LangMem 提取指令**（`src/langmem/knowledge/extraction.py` `_MEMORY_INSTRUCTIONS`）— Hive `extract_agent` 的标杆：把蒸馏建模成"维护 agent 的预测模型"，三步 Extract→Compare&Update→Synthesize，硬约束：**置信度 p(x)+推理标注** / **优先保留 surprising（偏离模式）+ persistent（反复强化）** / **maximize SNR、prefer dense over overlapping**。配套 episodic `Episode{observation, thoughts, action, result}` 四元 schema = 经验一等公民。

**agentmemory 高阶反思**（`src/prompts/reflect.ts:1` `REFLECT_SYSTEM`）— 反"信息聚合器"：`"Skip insights that merely restate a single source item"` + `"only becomes visible when viewing multiple memories together"`，强制跨记忆综合。

---

## D) MD-first 路线可借鉴设计（核心产出，对 Hive 最直接）

> Hive 铁律 = Everything is Markdown（MD 是唯一事实源）。三家走 MD-first：**basic-memory**（纯 MD 知识图谱）、**Tencent**（L2/L3 MD + 上层导航）、**claude-mem**（DB 主 + MD 可选渲染，但索引模式可借）。

### D.1 标签/分类体系 — 全行业共识：固定枚举骨架 + LLM 自由内容标签

| 维度 | 做法 | 谁 |
|---|---|---|
| **分类骨架**（决定文件/类型）| **固定枚举**（防漂移）：persona/episodic/instruction（Tencent）、semantic/episodic/procedural（LangMem）、6 类 type（agentmemory） | 多家 |
| **内容标签**（检索召回主键）| **LLM 自由生成** concepts/keywords/tags（无枚举） | A-MEM、agentmemory、Mem0 |
| **软枚举**（引导非硬约束）| 20 类 + "feel free to create new categories" | Mem0 OpenMemory |

→ **印证 Hive `[cat=]` 路线**：T2 早已"10 类压 3 文件"。**防增殖原则成立**：分类骨架用枚举、内容差异用条目标签，不为纯分类学差异开新文件。**knowledge.md + strategies.md 零运行时差异 → 应合并**（与本研究无关，是 Hive 既有实锤）。

### D.2 wiki 双链 — basic-memory 的纯 MD 微语法（直接可抄）

basic-memory 把知识图谱完全表达在 Markdown 里，无需离开 MD（`src/basic_memory/markdown/plugins.py`）：

```markdown
---
title: Hive Memory Engine
type: note
permalink: hive/memory-engine
tags: [memory, architecture]
---

## Observations
- [decision] 退役采用 de-index 而非物删 #lifecycle (源自神经科学 accessible→inaccessible)
- [fact] knowledge.md 与 strategies.md 零运行时差异 #audit
- 没有 category 的观察行也合法 #note

## Relations
- implements [[Compaction CC Alignment]]
- contradicts [[Legacy SQLite Shadow Store]]
- "depends on" [[Memory Write Gate]]   # 多词关系类型须引号

正文里任意 [[Knowledge Container Boundaries]] 引用 → 自动成为 links_to 关系
```

**解析规则**（`plugins.py`）：
- **Observation**：`- [category] content #tag (context)`；category 可选；排除 markdown 任务/链接/wikilink/Obsidian callout
- **Relation 显式**：list 项内 `relation_type [[target]] (context)`，多词类型须 `"..."` 引号
- **Relation 隐式**：正文任意 `[[target]]` → 类型 `links_to`
- **前向引用**：`[[Target]]` 可指向尚不存在的 note（`to_name` 先存，`to_id` 后解析）——**写时不阻塞，后台 LinkResolver 5 级 fallback 补链**
- **MD=源，SQLite=派生**：解析后写入 entity/observation/relation 三表，纯索引，删库可从 MD 重建

→ **Hive 落地**：T3 文件可引入这套 observation/relation 微语法，把"知识"从散文 bullet 升级为**可机械解析的图节点**，且**完全不离开 Markdown**。`[[wikilink]]` 解决 §9 的"记忆间无显式关联"，前向引用解决"写时不知道链到哪"。

### D.3 索引导航层 — Tencent 的"索引转正"答案（解 ④孤儿）

Hive `INDEX.md` 是孤儿（无消费方）。Tencent 给出闭环范本：

```
L2 scene_blocks/*.md（MD 源，含 META: created/updated/summary/heat）
      │ syncSceneIndex() 机械扫描（工程侧，LLM 看不到）
      ▼
.metadata/scene_index.json（shadow 索引）
      │ generateSceneNavigation() 渲染，按 heat 降序
      ▼
追加到 persona.md 的「🗺️ Scene Navigation」段：
      ### Path: /abs/path/scene_blocks/技术研究.md
      **热度**: 230 🔥🔥🔥 | **更新**: 2026-06-04
      Summary: 用户的 Rust 学习与系统编程转型轨迹
      │ persona.md 注入 agent 上下文
      ▼
agent 按图索骥 → read_file(绝对路径) 渐进披露加载全文
```

**与 Hive 孤儿 INDEX 的根本区别 = 索引有消费方**（经 persona.md 注入给 agent）。三个可直接抄的设计：
1. **索引渲染进常驻注入**（persona/soul 尾部），不是独立无人读的文件；
2. **每条带 `绝对路径 + heat🔥 + summary`** → agent 知道该 load 哪条、为什么（解 ⑤"注入不带理由"）；
3. **heat = 召回命中累计次数 = usage 遥测**（Hive S2 想要的），同时驱动**导航排序**和**合并牺牲者选择**（一份数据三用）。

claude-mem 的 `search → timeline → get_observations` 三步取详情、CC 的 `MEMORY.md 索引 + topic 文件` 是同一模式（index + 取详情 / 渐进披露），多家收敛=强信号。

### D.4 MD-first 的边界（哪些不该塞进 MD）

- **激活态/参数态记忆**（MemOS）天然非 MD（KV-cache/LoRA）——Hive 是纯明文态系统，**不碰这两态是对的**，别为"全 MD"硬做。
- **时态/审计的强结构**（Graphiti 的 bi-temporal 4 时间戳、MemOS MemCube governance 头）建议进 **frontmatter 结构化字段**，而非散文正文——MD 仍是源，但元数据要可机械查询。

---

## E) "蒸馏基于脑科学"假设的验证结论与修正建议

### E.1 成立的映射（可保留为设计依据）

1. **CLS 交错重放 → 巩固须新旧混合**：dream/heartbeat 不能只喂 delta（防灾难性遗忘）。✅ 有神经依据，且 Hive 当前增量游标是**可改进点**。
2. **主动遗忘可逆（accessible→inaccessible）→ 退役 = de-index 非物删**：MD 留档、移出活跃召回。✅ 与 MD-first 完美契合，Graphiti `expired_at` 是工程写实。
3. **干扰 vs 衰减两类遗忘 → 退役判据分两条**：干扰型（冲突消解/dedup）+ 衰减型（长期零召回归档）。✅
4. **离线系统巩固周期 → dream 的周期触发**：✅ 形状对。
5. **遗忘提升检索精度 → 容量 cap 防增殖的正当性**：✅ 激进退役是 feature。

### E.2 类比过度（应停止当"脑科学卖点"）

1. **给金字塔层起生物名**（soul=长期记忆 / focus=工作记忆 / behavior=情景记忆）= **MegGPT 级命名游戏**，换名机制不变，不构成"基于脑科学"的辩护。
2. **把 T0/T2 叫"情景/语义记忆"** = **Zep 级现象学借用**（半真）：有架构后果可以借，但**不能当脑科学证据**。
3. **"蒸馏=系统巩固"** 的强声称：Hive 蒸馏是纯明文压缩（MemOS plaintext 子集），**与突触/系统巩固的神经过程无机制对应**。

### E.3 脑科学给不出、必须工程自定的（诚实标注为"工程判据"）

- 升层分数阈值（w≥0.85）、容量上限（maxScenes、persona 2000 字）、巩固间隔（heartbeat 默认 120min eligibility、full dream 24h、soft dream 6h）、importance 评分——**全是工程拍的数，无脑科学背书**。Generative Agents 的 poignancy 1-10、Reflexion 的 Ω 容量界都是同类工程近似。

### E.4 修正建议（若要让"基于脑科学"真正站得住）

**唯一可借的真判据是 HippoRAG 路线**：不是贴生物名，而是从神经理论推出可证伪算法。两条可落地：
1. **检索侧（强）**：把 T3 知识做成 KG + Personalized PageRank 单步多跳（=海马体 pattern completion），替代 `retriever.py` 的打分召回——有 multi-hop benchmark 背书（+20.9% R@5）。**但这是重型方案**，当轮规格外（后由 MD-first P9 wikilink-KG+纯 PyPPR 落地；claude-mem-borrow 提案已于 2026-06-07 废除）。
2. **巩固侧（轻）**：node specificity（局部 IDF，海马体不做全局聚合）思想用到 T2→T3 重要性加权，替代全局统计。

**本轮规格的诚实定位建议**：把 Hive 记忆引擎定位为「**明文态多级蒸馏 + 巩固/遗忘原理启发的生命周期治理**」——结构受脑科学启发（§E.1 五条），但**判据是工程的、可观测的、可审计的**（符合 AI-Native L2 治理 + L3 控制中台）。不声称"神经写实"，把 HippoRAG 式 KG+PPR 列为**未来可选硬化路线**而非当前卖点。

---

## F) 对 Hive 八环节的借鉴清单（研究→规格的桥梁）

> 每环节：现状评级（来自 `knowledge-container-boundaries.md §9`）→ 借谁 → 怎么改。这是把本研究接入"环节全显式规格"的落地索引。

| 环节 | 现状 | 借鉴对象 | 落地建议 |
|---|---|---|---|
| **①写入 T0→T2** | ✅ | LangMem `_MEMORY_INSTRUCTIONS` | 补**置信度 p(x)** + **surprising/persistent 优先保留**进 extract_agent；引入 **Episode{obs/thoughts/action/result}** 四元结构化经验单元 |
| **②蒸馏 T2→T3** | ✅ | Tencent L2 Architect + LangMem | 已 SOP-driven；补"**禁止列表追加、要连贯叙事**"反约束（=RC15 解法）；交错重放（新旧混合输入，非纯 delta） |
| **③归档（选文件）** | 🔴 模糊 | 全行业共识 + basic-memory | **固定枚举骨架 + `[cat=]` 自由标签**；**knowledge+strategies 合并**（既有实锤）；防增殖原则定为正式边界 |
| **④索引** | 🔴 孤儿 | **Tencent scene-navigation** + claude-mem | INDEX 转正为**注入 soul/focus 尾部的导航层**：每条 `路径+heat+summary`，agent 渐进披露 read_file |
| **⑤系统检索** | ⚠️ | Generative Agents 三因子 + Tencent | 注入条目**带召回理由**（goal/owner/keyword/heat）；打分补 **recency 衰减 + importance** |
| **⑥AI 主动查** | ⚠️ | Letta 工具 description | 补**主动查询触发判据**（任务涉及"过去决策/用户偏好/曾失败/具体人或项目"→先 search_memory） |
| **⑦维护（dedup/降级）** | ⚠️ | **Mem0 控制器** + Tencent L1 dedup + Letta | 加**结构化逐条裁决层**：输入候选+检索到的既有记忆，输出 `{id, event, old_value, reason}` 落 evolution_ledger；借 Letta **唯一性守卫**（改 MD 不踩空）+ **chars 容量可见** |
| **⑧退役** | 🔴 缺失 | **Graphiti bi-temporal + Tencent 容量 cap + agentmemory auto-forget** | **退役=de-index 非物删**（MD 留档+移出召回）；**容量 cap 强制合并**（heat 最低先合）；判据分两条（干扰型冲突消解 + 衰减型长期零召回）；dream 决策非机械删（AI-native） |
| **S2 usage 遥测** | 规划中 | **Tencent heat** | `access_log.py` 升级条目级 `recall_count/last_recalled_at`=heat，喂 dream 的退役候选 + 导航排序（一份数据三用） |

---

## 附录：引用清单

**论文**：MemGPT [2310.08560](https://arxiv.org/abs/2310.08560) · Generative Agents [2304.03442](https://ar5iv.labs.arxiv.org/html/2304.03442) · A-MEM [2502.12110](https://arxiv.org/abs/2502.12110) · Reflexion [2303.11366](https://arxiv.org/abs/2303.11366) · Voyager [2305.16291](https://arxiv.org/abs/2305.16291) · HippoRAG [2405.14831](https://arxiv.org/abs/2405.14831) · Zep [2501.13956](https://arxiv.org/abs/2501.13956) · MemOS [2507.03724](https://arxiv.org/abs/2507.03724) · Agent Memory Survey [2603.07670](https://arxiv.org/html/2603.07670v1)

**脑科学**：CLS [McClelland et al. 1995](http://wixtedlab.ucsd.edu/publications/Psych%20218/McClellandMcNaughtonOReilly95.pdf) · 遗忘综述 [Nat Rev Neurosci s41583-021-00548-3](https://www.nature.com/articles/s41583-021-00548-3) · 主动遗忘 [Neuron S0896-6273(17)30498-1](https://www.cell.com/neuron/fulltext/S0896-6273(17)30498-1) · 遗忘可逆 [eLife 92860](https://elifesciences.org/articles/92860)

**开源项目**（克隆源码深读，2026-06-04）：
- TencentCloud/TencentDB-Agent-Memory — `src/core/prompts/{l1-extraction,l1-dedup,persona-generation,scene-extraction}.ts`、`src/core/scene/{scene-format,scene-index,scene-navigation}.ts`（L0→L3 金字塔，与 hermes 生态绑定，PersonaMem 48%→76%）
- basicmachines-co/basic-memory — `src/basic_memory/markdown/{plugins,schemas,entity_parser}.py`（纯 MD 知识图谱）
- mem0ai/mem0 — `mem0/configs/prompts.py`、`mem0/memory/main.py`（ADD/UPDATE/DELETE/NOOP + V3 退化）
- getzep/graphiti — `graphiti_core/prompts/{extract_nodes,extract_edges,dedupe_edges}.py`、`utils/maintenance/edge_operations.py`（bi-temporal 失效）
- letta-ai/letta — `letta/prompts/system_prompts/sleeptime_v2.py`、`letta/functions/function_sets/base.py`（自编辑+sleep-time）
- langchain-ai/langmem — `src/langmem/knowledge/extraction.py`、`src/langmem/prompts/{gradient,metaprompt}.py`（三分+prompt optimizer）
- rohitg00/agentmemory — `src/prompts/{compression,consolidation,reflect,graph-extraction}.ts`、`src/functions/{retention,auto-forget}.ts`（5 段蒸馏+Ebbinghaus）
- claude-mem（本地插件）— observation 压缩 + index/get_observations + type×concept 治理信号

**研究方法注记**：deep-research workflow 因 rate-limit 致对抗验证阶段 100% StructuredOutput 失败、误报"all refuted"——59 条一手论断实为**未验证非被驳倒**，已由 7 路源码深读 agent 补全验证。GitHub 仓库 WebFetch 普遍失败，改用 clone 源码直读。
