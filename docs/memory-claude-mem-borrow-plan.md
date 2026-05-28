# 记忆系统借鉴 claude-mem 的优化提案

> 状态: 提案 / 草案 v2（2026-05-28 fresh scan 后修订；决策未定，② 语义召回需拍板后再实施）
> 作者: Claude (Opus 4.7) — based on 2026-05-27 跨仓双 agent 审计 + 主 Claude 亲验 claude-mem `prompts.ts`/`AgentFormatter.ts`/`code.json`
> 对比对象: `/Users/rocky243/vc-saas/claude-mem`（thedotmack/claude-mem，Claude Code 记忆插件）
> 范围: `backend/app/memory/*` + `backend/app/services/{extract_agent,heartbeat,auto_dream,t0_logger}.py` + `backend/app/tools/handlers/memory.py`
> 证据说明: 行号以 2026-05-27/28 当时代码为准；claude-mem 侧引用同理。实施前请按符号重新定位，勿盲信行号。
> 2026-05-28 修订说明: 本版纠正 Hive 侧过期判断：`extract_queue`/startup replay 已存在；auto-dream gate 已落盘；heartbeat KAIROS 仍以内存 cache 为主但消息写入 DB；Hindsight 是已有可选语义后端但不是默认热路径。
> North Star 对齐: 本提案服务 Goal 1（自我进化 agent 内核，bar = 超越 `hermes-agent`），优化记忆的**召回质量**与**token 经济性**，不得回退现有治理/灵魂/多租户能力。

---

## 0. TL;DR

- 你每个 session 开头看到的那段 `# [hiveclaw/hiveclaw-main] recent context ... Legend: 🎯session 🔴bugfix ... 92% savings ... Access 236k tokens of past work via get_observations([IDs])` —— **那就是 claude-mem 的输出**，是你开发 Hive 时用的记忆插件。它"处理得好"你是亲眼见过的。
- 本文对比对象因此很明确：**claude-mem（开发态记忆插件）** vs **Hive 产品自带的 4 层 MD 金字塔 agent 记忆**。
- claude-mem 的核心优势可压成两句：(1) **prompt 里始终是一张"记忆地图"（索引 + 按 ID 取详情），不是全文**；(2) **字段级 embedding + 混合检索**让召回按"意思"而非"字面"命中。
- Hive 现状两大短板正好对应：(1) prompt 里仍以**可执行内容注入**为主：`focus.md` 整篇、P0 `feedback.md`/`blocked.md` 全量逐条、P1/P2 逐条直注；已造好的 `INDEX.md` 只是文件级索引且生产热路径默认不用；(2) 默认 MD 热路径缺少真正语义召回，主要是 BM25/词重叠/字符重叠，虽已有 Hindsight 可选后端但不是默认统一排序层。
- 借鉴优先级：**① entry-level 记忆索引 + 取详情工具（先保 P0 全量/近全量，折叠 P1/P2）→ ② 语义召回统一层（优先接现有 Hindsight，再决定本地 embedding）→ ③ 队列/cursor/replay 一致性与 heartbeat cache 可恢复性 → ④ 成本记账 + 双轴标签（策展增强）**。

---

## 1. 框架澄清：两套"记忆"不是一回事

| | claude-mem | Hive 自带记忆 |
|---|---|---|
| 角色 | Claude Code 插件，**开发态**：记录"我（开发者）写 Hive 的过程" | **产品功能**：交付给企业数字员工的长期记忆 |
| 存储 | SQLite + Chroma 向量 | MD 文件为真相源（旧 SQLite shadow 已退役） |
| 单元 | observation（压缩后的结构化"发生了什么"） | T0 原始日志 → T2 learnings → T3 memory → soul |
| 治理 | 仅 `<private>` 边缘剥离 | PL4 拒绝、敏感度分级、owner/company 上下文、soul 进化、多租户 RLS |
| 注入 | SessionStart 注入索引 + 图例；按 ID 取详情 | retriever 把 4 层打分后整段灌入 prompt |

**结论**：claude-mem 在「**注入形态 + 召回机制**」这两个工程维度上确实更精炼；Hive 在「**治理 + 身份进化 + 多租户**」上远超它。借鉴只取前者，绝不回退后者。

---

## 2. claude-mem 记忆机制速览（被借鉴方）

> 主 Claude 亲验项标 ✓；其余来自 2026-05-27 Explore agent 深度审计。

1. **observation = 压缩后的"系统状态变更"，不是 transcript**。PostToolUse 把单次工具 I/O 包成 XML（`buildObservationPrompt`，`src/sdk/prompts.ts:81`），喂给一个**独立的 observer SDK 会话**异步压缩。压缩指令外置在版本化的 **mode 文件**（✓ `plugin/modes/code.json:100-138`），核心规则："记录 LEARNED/BUILT/FIXED，不是你自己在干什么"；跳过就返回空、不要解释。
2. **双轴标签**：`type`（改了什么：✓ `code.json` 里 bugfix🔴/feature🟣/refactor🔄/change✅/discovery🔵/decision⚖️/security_alert🚨/security_note🔐）正交于 `concept`（什么知识：how-it-works/why-it-exists…）。解析器会主动把 type 从 concept 里剔重（`src/sdk/parser.ts:119`）。
3. **schema 关键字段**（`src/services/sqlite/schema.sql`）：`observations` 表带 `type/title/subtitle/facts/narrative/concepts/files_read/files_modified`，外加 **`discovery_tokens`**（当初挖出来花了多少 token）、**`content_hash`**（`UNIQUE` 去重，非时间窗）。原始 I/O 进**独立的持久队列** `pending_messages`，靠 `UNIQUE(content_session_id, tool_use_id)` 幂等。
4. **注入 = 两段式**（✓ `src/services/context/formatters/AgentFormatter.ts`）：
   - SessionStart（无 query）：**最近 N 条全文 + 其余全部塌缩成一行索引**（`renderAgentTableRow:90` → `id time icon title`）+ 一段**图例**教模型 `get_observations([IDs])`（`renderAgentLegend:31`）+ footer "Access Nk tokens of past work via get_observations([IDs])"（`:162`）。
   - UserPromptSubmit（有 query）：可选语义注入，prompt≥20 字符才触发。
5. **字段级 embedding + 混合检索**：每条 fact 单独入 Chroma（`src/services/sync/ChromaSync.ts:102`），查询命中原子事实再 dedupe 回父记录。需要精确表述：普通 query path 可走 Chroma-first 后 SQLite hydrate；concept/type/file 等 metadata-filtered path 才是更典型的**SQLite 资格过滤 → Chroma 排序 → hydrate**（`HybridSearchStrategy.ts:111`）。这仍然比纯 BM25 更贴近"意思"。
6. **三段式检索工作流**做成一等公民（MCP `src/servers/mcp-server.ts`）：`search()` 出 ID+标题 → `timeline(anchor=ID)` 出时间邻居 → `get_observations([IDs])` 批量取全文。每步标注 token 成本，硬规则"先过滤再取详情，10x 节省"。
7. **token 经济学**：每条记 `discovery_tokens`，读取成本 = 压缩后大小；`savings% = (discovery − read)/discovery`（`TokenCalculator.ts`）。捕获同步轻量（只入队），压缩异步出带（worker），**不进用户关键路径**。

---

## 3. Hive 记忆现状与差距（我方）

> 来自 2026-05-27 Explore agent 对 `backend/app/` 的现状审计。

1. **T0 捕获**（`services/t0_logger.py`）：纯模板、零 LLM，按 `behavior|system|artifacts` 分目录写 MD。截断是破坏性的（text 5000 / result 3000 字符），仅 >8000 字符的结果才 spill 到 `artifacts/`。
2. **T0→T2 抽取**（`services/extract_agent.py`）：LLM 主 + regex 兜底，prompt 重度工程化（9 类目、反注入、`NOTHING` 哨兵）。当前 hot path 已有 `extract_queue`：`schedule_extract()` 会先把 payload 写到 `{AGENT_DATA_DIR}/.failed_extractions`，任务成功再 `mark_done()`，startup 通过 `extract_queue_replay.py` 重放未完成项。仍然存在的短板是：`_cursors` 仍是进程内 `dict`，queue 的幂等粒度是 scheduled payload，不是稳定的 `session_id + message_id/tool_use_id` cursor 真相源；重复/漏抽主要靠 `append_t2_entries` 内容去重和 T0 backfill cursor 兜底。
3. **T2 存储**（`memory/t2_store.py`）：权重 = source-bucket × category（`compute_t2_weight`）。**无时间衰减**（模块名带 decay 但权重是静态的），只有 `repeat` 计数和 heartbeat 策展提供老化压力；去重是精确归一化（whitespace+lowercase），**改写句会作为重复累积**。
4. **T2→T3 策展**（`services/heartbeat.py`）：LLM 驱动的 KAIROS 风格连续 heartbeat。调度 loop 是 60s tick，但 per-agent 默认 heartbeat interval 是 45min。运行时上下文 `_heartbeat_contexts/_heartbeat_session_ids/_heartbeat_tick_counts/_t2_mtimes/_heartbeat_session_ctxs` 仍以内存 cache 为主；同时 heartbeat 会写 `ChatSession/ChatMessage`，所以"消息完全丢失"不准确，准确短板是**重启后不会把 DB 中的 heartbeat session 自动恢复回 KAIROS cache**。T3 去重参考仍被截到 500 字符，模型可能 append 看不见的近重复。
5. **T3→soul**（`services/auto_dream.py`）：LLM 主 + 纯 Python pattern 兜底，门控"4h + (3 sessions 或 heartbeat tick gate)"。`auto_dream_state.json` 已持久化 `last_dream_time/sessions_since_dream/version/history`，所以不能再说 dream gate 重启即丢；但 heartbeat tick counter 仍是内存态，soul 去重是子串包含，**改写的身份行会漏过**。
6. **检索注入**（`memory/{retriever,activation,assembler}.py` + `services/memory_service.py`）：**关键短板**——
   - 默认 MD 热路径是关键词/recency/逐 bullet，没有内置 embedding。`activation.py` 的 `goal_relevance`/`open_loop_pressure` 是字面 token 重叠（`_overlap`）。后果："billing" 召不回写成 "invoice" 的记忆，除非租户启用了 Hindsight 且命中其 read-side accelerator。
   - `focus.md` **整篇按 1.0 分注入**（无相关性门）；`feedback.md`/`blocked.md` 作为 P0 每条 bullet 全量进 prompt。P0 保真是合理的安全设计，但也带来随时间膨胀风险；P1/P2 目前仍以逐条内容进入 snapshot，而不是一行索引。
   - activation 只会**加分不减分**，排序信号弱。
   - 唯一向量能力是外部 `Hindsight` 后端（`memory/backends/hindsight.py`），默认关闭、按租户开。
7. **INDEX.md**（`memory/md_store.py:272`）：每次 T3 append 都重建，但当前只是**文件级**索引（file/category/items/updated/load），不是 entry-level manifest。`_retrieve_t3_index_first` 已存在并有 shadow test，但被 `use_t3_index_first` 门控，生产调用方 `memory_service.py` 默认 `False`。即**已造好但粒度不够，且生产热路径未启用**。
8. **agent 自查工具** `search_memory`（`tools/handlers/memory.py:183`）：也是 BM25（`search_t3_facts`），**连 agent 自己的召回也是字面匹配**。

---

## 4. 可借鉴点（按 ROI 排序）

### ① 记忆"索引 + 按 ID 取详情" — 最该抄，但要保 P0 边界

- **claude-mem 怎么做**：prompt 常驻一份一行式索引（`id time icon title`）+ 少量最近全文 + 图例教模型 `get_observations([IDs])`；老条目永远只占一行，要用才展开。
- **Hive 现状**：P0 全量注入、P1/P2 逐条内容注入；`INDEX.md` 已造好但只是文件级索引且热路径默认关闭。
- **借鉴方案**：Hive **早有同构范式**——技能就是"目录进 prompt + `load_skill` 取全文"。把它复刻到记忆：
  1. 先把 T3 bullet 生成 **entry-level manifest**（稳定 id/content_hash + title/preview + category + source file + timestamp + sensitivity + weight/access stats），不要只用现有文件级 `INDEX.md`；
  2. prompt 常驻：P0 最近/高权重条目仍可全文保留，P0 旧条目折叠成索引；P1/P2 默认索引化，只展开少量高相关项；
  3. 加 `load_memory(ids)` 工具按需展开全文（对齐 `load_skill`，必须支持 batch ids）；
  4. `search_memory` 返回 ID+标题+preview，引导 agent 先过滤再 `load_memory`。
- **附带红利**：claude-mem"老条目塌缩成一行 + recency 排序"本身是一种**优雅的衰减替代**（不删不降权、只折叠），可缓解 Hive P0/P1/P2 膨胀；但 P0 不能一刀切折叠，否则可能隐藏用户纠正/失败模式。
- **价值/成本**：高价值 / 中低成本（复用已有 skill progressive-disclosure 模式，但需要新增 entry id/schema）。**建议首先做。**
- **落点**：`memory/md_store.py`（entry-level manifest / stable id）、`memory/retriever.py`（索引层 + opt-in 到 production）、`memory/assembler.py`（预算分配给索引/全文混合）、`tools/handlers/memory.py`（新增 `load_memory` + `search_memory` 返回 id）、工具 registry/decorator 自动注册链路、`runtime/prompt_sections/memory.py`（渲染索引 + 图例）。

### ② 进程内语义召回 — 对"体感弱"杀伤最大，需拍板

- **claude-mem 怎么做**：字段级 embedding（每条 fact 单独入库）+ 混合检索（lexical 过滤 → 语义排序，交集 + rerank）。
- **Hive 现状**：默认 MD backend 无语义召回；已有 Hindsight 可选后端，按 tenant opt-in，并通过 `hindsight_sync.py` 把 T3 markdown 同步为 derived index，但它还不是统一默认排序层。
- **借鉴方案**：T3 的 bullet 很短，**天然适合字段级 embedding**。务实路径不是立即引入新依赖，而是先把语义能力抽成统一接口：BM25/metadata 做候选过滤，Hindsight 或本地 embedding 做语义排序，最后喂给 `ActivationScorer`。`search_memory` 工具同步升级为"索引结果 + 可 batch load"工作流。
- **决策点（实施前必须拍板）**：
  - **方案 A**：先复用已有 `Hindsight` 外部后端并强化为统一 semantic reranker，不引入新进程内依赖，但运维、可用性、跨租户隔离要验证；
  - **方案 B**：新增进程内轻量 embedding（如 sentence-transformers / 本地小模型 / LLM embedding API），新增向量索引文件，破 MD-first 纯净度但自洽可控。
- **价值/成本**：最高质量杠杆（直接对标 hermes-agent 体感）/ 改动最大（涉及在 MD-first 之外引入向量）。**建议②做，但先决策 A/B。**
- **落点**：`memory/activation.py`（语义信号接入打分）、`memory/retriever.py`（召回层）、`memory/backends/*`（向量索引实现或 Hindsight 强化）、`tools/handlers/memory.py`（`search_memory` 混合化）。

### ③ 队列/cursor/replay 一致性 + heartbeat cache 可恢复性 — 生产可靠性

- **claude-mem 怎么做**：PostToolUse 只把原始 I/O 入持久队列，靠 `UNIQUE(session, tool_use_id)` 幂等；LLM 压缩异步出带。
- **Hive 现状**：抽取已异步，且 `extract_queue` + startup replay 已覆盖 task crash / deploy restart / drain timeout；`auto_dream` gate 已部分落盘。剩余可靠性缺口是：`extract_agent._cursors` 仍是进程内 message index，queue entry 没有 `session_id + message_id/tool_use_id` 级别幂等键；heartbeat KAIROS cache 重启后不从 DB session 恢复；heartbeat tick counter 仍是内存态。
- **借鉴方案**：用"持久 cursor/claim ledger + 幂等键（session_id + message_id/tool_use_id）"替代单纯内存 cursor，让 queue/cursor/replay 共享同一真相源；heartbeat 从 `ChatSession/ChatMessage` 恢复最近 KAIROS 上下文或显式写可恢复 checkpoint；auto_dream 补齐 heartbeat tick state。
- **价值/成本**：中高价值（对齐 production-ready）/ 中成本。**建议③在①之后做。**
- **落点**：`services/extract_agent.py`（cursor 落盘 / queue idempotency）、`services/extract_queue.py` 与 `services/extract_queue_replay.py`（稳定幂等键）、`services/heartbeat.py`（KAIROS checkpoint/恢复）、`services/auto_dream.py`（补齐 tick state）。

### ④ 成本记账 + 双轴标签 — 喂给更聪明的策展

- **claude-mem 怎么做**：每条记 `discovery_tokens`；标签双轴（type × concept），解析器主动剔重。
- **Hive 现状**：有 `[w=][src=][cat=]` 与 9 类目，但无 token 成本记账、无双轴。
- **借鉴方案**：(a) T2 entry 增记"挖掘成本"近似值，喂给 curator/dream 做**"贵的记忆优先留"**的保留决策（比 vanity 指标有用）；(b) 引入"知识维度"第二轴（how-it-works / why-it-exists / gotcha…），让 ① 的索引可按维度过滤。
- **价值/成本**：中价值 / 中低成本。**建议④后置，作为①②③稳定后的策展增强。**
- **落点**：`memory/t2_store.py`（metadata 扩展）、`services/extract_agent.py`（prompt 增第二轴输出）、`services/{heartbeat,auto_dream}.py`（保留决策用成本）。

---

## 5. 不要抄的 / Hive 已领先

- **治理 / 灵魂 / 多租户**：claude-mem 隐私只有一个 `<private>` 边缘剥离；Hive 有 PL4 拒绝、敏感度分级、owner/company 上下文、soul 进化、RLS。重构记忆时**这些是不变量，不得回退**。
- **"加时间衰减"不算从 claude-mem 学的**：它本身也没有真正的 decay，是靠 recency 排序 + 老条目折叠回避的。所以解 Hive 的膨胀问题应优先走 ①（索引折叠 + 预算分层），不要先单独造 decay 引擎。
- **observer 用独立 SDK 会话**：claude-mem 的 observer 是另起会话异步压缩；Hive 的 extract 已是 hook 异步，不必照搬"独立 agent 会话"这层复杂度。

---

## 6. 建议路线（分阶段，决策门已标注）

| 阶段 | 内容 | 风险 | 前置决策 | 验收（建议） |
|---|---|---|---|---|
| **A** | ① entry-level 记忆索引 + `load_memory(ids)` 工具 + P1/P2 默认折叠；P0 保持全文/近全文安全边界 | 中低 | 定 entry id/schema 与 P0 折叠策略 | 索引层注入 token < 原全文注入；agent 能按 id 批量取回全文；P0 用户纠正/blocked pattern 不丢；回归 `pytest tests/memory tests/runtime` 绿 |
| **B** | ② 语义召回统一层（先接 Hindsight 或新本地 backend）+ `search_memory` 混合化 | 中高 | **拍板 Hindsight-first vs local embedding** | "billing"↔"invoice" 类跨措辞召回用例通过；metadata 过滤 + semantic rerank 正确；与现有打分不回归 |
| **C** | ③ 抽取 cursor/queue/replay 统一幂等键；heartbeat KAIROS checkpoint/恢复 | 中 | 无 | 重启后不丢 cursor、不重复抽取；幂等键去重；heartbeat 可恢复最近上下文或明确从 checkpoint 续跑；故障注入测试通过 |
| **D** | ④ 成本记账 + 双轴标签，喂保留决策 | 低 | 无 | T2 带成本与第二轴；curator/dream 保留逻辑可解释；索引可按维度过滤 |

> 落地顺序建议 **A → C → B → D** 或 **A → B → C → D**：A 最先（立竿见影、零决策）；B 是质量主轴但需拍板；C 提升可靠性可与 B 并行/穿插；D 收尾。

---

## 7. 待决策清单（动手前需明确）

1. **②的 A/B 方案**：Hindsight-first（复用现有后端、运维/隔离要验）vs local embedding（新增依赖/索引文件、MD-first 纯净度下降）。**这是本提案最大的一个决策。**
2. **索引层的 id 稳定性**：T2/T3 bullet 当前无统一稳定 id，`load_memory(ids)` 需要先给每条 bullet 分配稳定标识（content_hash 或 append-time id），需定 schema。
3. **P0 折叠策略**：`feedback.md`/`blocked.md` 是安全相关高优先记忆，不能为了 token economics 全量折叠；需定"最近 N 条全文 + 旧项索引"或"高权重全文 + 低权重索引"。
4. **索引 token 预算**：常驻索引层占 prompt 多少（claude-mem 用"最近 N 条全文 + 其余一行"，Hive 需定 N 与折叠阈值）。
5. **嵌入模型来源**：若走 local embedding，用本地模型 / LLM embedding API / 其他，涉及成本与延迟。

---

## 8. 附录：关键 file:line 索引

**claude-mem（被借鉴方，`/Users/rocky243/vc-saas/claude-mem`）**
- 压缩 prompt：`src/sdk/prompts.ts:81`；mode 配置 ✓ `plugin/modes/code.json:1-138`
- 解析/双轴：`src/sdk/parser.ts:15`（schema）、`:119`（type↔concept 剔重）
- schema：`src/services/sqlite/schema.sql`（`observations` / `pending_messages` / `discovery_tokens` / `content_hash`）
- 注入渲染 ✓：`src/services/context/formatters/AgentFormatter.ts:31`（图例）、`:90`（索引行）、`:162`（footer）
- 混合检索：`src/services/worker/search/strategies/HybridSearchStrategy.ts:111`（metadata-filtered rerank path）、`src/services/worker/search/strategies/ChromaSearchStrategy.ts` / `SearchManager.ts`（普通 semantic query path）；字段级 embedding `src/services/sync/ChromaSync.ts:102`
- token 经济：`src/services/context/TokenCalculator.ts`
- MCP 三段式：`src/servers/mcp-server.ts`

**Hive（我方，`backend/app/`）**
- T0：`services/t0_logger.py`
- 抽取：`services/extract_agent.py`（cursor 内存态）、`services/extract_queue.py` / `services/extract_queue_replay.py`（durable queue + startup replay 已存在）
- T2 存储/权重：`memory/t2_store.py`（`compute_t2_weight`）
- 策展：`services/heartbeat.py`（KAIROS cache 内存态，但 DB session/messages 持久化）
- dream：`services/auto_dream.py`（`auto_dream_state.json` 已持久化 gate state）
- 检索/激活/装配：`memory/retriever.py`、`memory/activation.py`（`_overlap` 字面匹配）、`memory/assembler.py`、`services/memory_service.py:101`（`use_t3_index_first=False`）
- 索引（文件级 + 默认未启用）：`memory/md_store.py:272`、`memory/INDEX.md`、`memory/retriever.py::_retrieve_t3_index_first`
- 向量后端（可选，默认 MD）：`memory/backends/hindsight.py`、`memory/backend.py`、`memory/hindsight_sync.py`
- agent 自查工具：`tools/handlers/memory.py:183`（BM25）
- 范式先例：技能"目录 + `load_skill`"见 `skills/` + `tools/handlers/skills.py`
