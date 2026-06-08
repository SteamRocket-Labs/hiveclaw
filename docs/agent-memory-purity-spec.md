# Agent 记忆纯净度 — 技术债清册 (Memory Purity: Spec-Compliance Debt Register)

> **单一核心**：让生产里的持久记忆回归「纯净」—— 身份级、可读、有界、无遥测污染、无 episodic 漂移。
> **方法**：**不重设计架构**。我们已有的 `docs/agent-memory-md-first-spec.md`（P0-P10）就是正确架构。本文做的是**逐项核对生产与该 spec 的偏离，把偏离当技术债清掉**。
> **状态**：v0.3。owner 已拍板（2026-06-08）：**D1-D10 单次完整交付，禁 MVP，不留债**（见 CLAUDE.md/AGENTS.md「交付纪律」）。进入实现。

---

## 0. 结论先行

Owner 判断（2026-06-08）：

> "架构上没什么太大的问题，改进空间并不明显。它肯定是有很多小问题，或者说我们之前改造升级后遗留下来的技术债务没清，才导致整个结构出了问题。"

**核对 spec 后，这个判断 100% 成立，且有逐条证据。** P0-P10 的 spec 是对的——它**明确定义甚至明令禁止**了生产里的每一种「脏」：

- §3.2 说 metadata 该进 **sidecar/frontmatter**，inline 只是「首版可接受的临时做法」。
- §4.4 说 **prefer update/merge，capacity caps，avoid simple bullet append**。
- §4.8/§4.9 定义了 supersede/archive/decay/retirement。
- §7 说 `knowledge.md`/`strategies.md` **不要在没迁移的情况下合并**（→ 我们自己的 spec 就反对「收敛 2 层」式重设计）。
- §8 说 INDEX **不能是孤儿镜像**，soul **必须是身份不是导航**。

生产偏离了这些。偏离的成因（结合记忆时间线）：**P0-P10 是 2026-06-04/05 才实装的，而生产 agent 数据从 4 月就在攒。** 实装走了 spec 允许的「首版捷径」，但债没还、老数据没回填、§4.4/§4.8/§4.9 的 consolidation/retirement 没真正接通。

→ **本文不提任何架构改动。只清债。**

---

## 1. 生产实证（Railway production, `/data/agents`, 2026-06-08）

抽样最脏 agent **「展会雷达」(`6d6605f2`)** + 交叉验证 (`5f6ec3c3`)。摘要：

- `strategies.md` 24KB/51 条，几乎全是 `2026-06-04 17:00 midday_scan: 窗口内14个展会无变化` 流水账；真策略仅 2-3 条。第二个 agent 的 strategies.md 是「人事变动数据库」——lane 违约**跨 agent 系统性**。
- `<Phone_1>:00 evening_scan`：脱敏器把时间 `17:00` 当电话号码 redact，公开日志被盖 `PL2_pii`。
- 半文件格式漂移：2026-05-25 前干净 `[date] content`，之后全带 `[entry_id=][sensitivity=][access_count=97][last_accessed=...]`。
- `soul.md` 自相矛盾：Mission「每日三次扫描+主动推送」vs dream 提升的 Learned Behaviors「禁用三次扫描，改每周五一次」；流水账证明当天仍扫 3 次。
- `INDEX.md` 38KB（每条 entry 全量镜像）+ 第二索引 `MEMORY_INDEX.md`；`lifecycle.json` 80KB；`memory.sqlite3` 仍在（规格已退役 SQLite）；`reflections.md` 多 agent 均 616B 死桩，部分建成了目录。

---

## 2. 技术债清册（生产 ✗ vs spec ✓）

每一项：生产现象 → spec 怎么说 → 债的来源 → 修法 → 可逆性。

| ID | 生产现象（✗） | spec 怎么说（✓） | 债的来源 | 修法 |
|----|---------------|------------------|----------|------|
| **D1 遥测盖进正文** | `[access_count][last_accessed][sensitivity]` 内联进 `.md`（`access_log.py:18-58`），每次读取重写文件 | §3.2「frontmatter or **sidecar manifest** rather than rewriting every existing bullet」；P6 telemetry | P2/P6 首版捷径，未还。`lifecycle.json` sidecar **已存同一份**（`lifecycle_store.py:32,219`）→ 冗余 | 停止内联盖章；relevance 评分只从 `lifecycle.json` 读；`.md` 回归纯 prose |
| **D2 半文件未回填** | 05-25 前后两种格式并存 | §3.2 允许首版不回填，但**这是临时态** | P2/P6 cutover 无 backfill migration | 一次性规范化（结合 D1：剥离全部内联 metadata → 纯 prose），原文进 `archive.md` |
| **D3 纯 append 无合并** | save_memory/curation 直接追加 bullet；6 条重复 SIEAR；strategies.md 流水账 | §4.4「prefer **update/merge**… **capacity caps to force merge**… **avoid simple bullet append**」 | §4.4 anti-proliferation **从未对 T3 `.md` 实装** | 写入前对同主题既有条目 merge/update；每 store 设软上限触发强制合并 |
| **D4 无 reconsolidation/retirement** | 文件只增不减；`archive.md` 存在但没被喂 | §4.8 supersede/archive/stale；§4.9 decay 双 lane（可逆）| lifecycle patch lane 定义了但没接通到真正收缩 T3 | heartbeat/dream 产出 lifecycle patch → 真正 de-index + 归档 |
| **D5 save_memory 越 lane** | agent 自选 category，episodic 直灌 durable（`memory.py:118`，无 lane 门）| §5「should not bypass governed T3 append」；P2 governed append | save_memory 只治理 sensitivity/PL4，**不治理 lane/episodic** | 走与 extractor 同一 lane 校验；episodic 拒绝并回执「应存 workspace/T0」|
| **D6 dream 矛盾门盲点** | soul 出现与 Mission 矛盾的 Learned Behavior | §5「does not bypass owner/charter gates」；§4.6 soul=identity | 矛盾门只查 T3-vs-T3，漏 promotion-vs-frozen-Mission | 扩展矛盾门：晋升候选必须与 soul frozen Mission/charter 比对 |
| **D7 INDEX 镜像 + 双索引** | `INDEX.md` 38KB 全量镜像 + 孤儿 `MEMORY_INDEX.md` | §8「should not be orphan… runtime consumer」；轻量 nav 行；soul≠navigation | INDEX 退化成镜像；第二索引无消费者 | INDEX 改轻量 nav（id/path/summary/heat）；删冗余第二索引 |
| **D8 SQLite 退役未清** | `memory.sqlite3`+`memory.json` 仍在 | §9 索引可重建非写路径；CLAUDE.md「SQLite shadow store 已退役」 | 退役没删文件 | 确认无写入后移除/忽略（低优先，无害但脏）|
| **D9 PII 误杀腐蚀正文** | `17:00`→`<Phone_1>`；公开日志标 PL2_pii | §4.2 sensitivity hints 应辅助非破坏内容 | redactor/分类器正则过激 | 修脱敏正则（时间/编号不当电话）；分类器收紧；机械步只兜底不毁内容 |
| **D10 死桩与结构不一** | `reflections.md` 616B 空模板；file vs dir 不统一 | §7 canonical T3 集不含 reflections | pre-spec 模板脚手架遗留 | 要么接通 writer，要么从模板移除；统一 file/dir |

---

## 3. 为什么这是「债」而不是「架构问题」

- **抽取 prompt（`extract_agent.py:96-267`）是 L1 质量**：有 `<what_to_skip>`、`<thinking_instruction>`、明令跳过 ephemeral——纪律已经写对了。脏在它**没覆盖到的写入路径**（save_memory / curation / dream）。
- **spec 的 lifecycle（§4）九阶段、anti-proliferation（§4.4）、retirement（§4.9）、sidecar metadata（§3.2）全是对的设计**。生产没按它跑，是实装欠账。
- **时间线坐实**：P0-P10 在 6-04/05 落地，老数据 4 月起攒 → 05-25 格式突变 = 上线那一刻 → 老数据没回填 = 典型「升级遗留债」。

一句话：**不是地基错了，是装修队上完新管子没拆旧管子、没填墙、没接通水泵。**

---

## 4. 正面回答 Owner 原来的两个问题

**Q1 — 三层蒸馏是否太臃肿 / 要不要收敛到 2 层？**
**不收敛。** 我们自己的 spec §7 明确：`knowledge.md`/`strategies.md`「Do not immediately merge them without a migration」，合并只在「runtime/prompt/dream/tests/migration 全部一致后」才考虑。三层蒸馏（extractor→heartbeat→dream）在 §5 有清晰的 authority boundary，架构成立。**臃肿感来自 D3/D4（没合并、没退役）让每层产物无界膨胀，不是层数本身。清 D3/D4，臃肿感消失。**

**Q2 — soul.md 谁更新 + 写法限制？**
dream 仍是写入者（§5 IdentityPromoter），但现状矛盾门有盲点（D6）。修法：dream 提 diff candidate → 扩展矛盾门含「vs frozen Mission」→ soul 有界且 §8「soul≠navigation」。**主动更新能力**：对标 `claude-md-management` 的 `claude-md-improver`（对照模板审计 → 定向改写），给 owner/agent 一个显式触发的 soul 审计入口，而非只能等 dream 被动跑。

---

## 5. 执行序（单次完整交付，非 MVP 停顿点）

> **交付纪律**（CLAUDE.md/AGENTS.md）：**D1-D10 一次性改完，禁 MVP，不留债。** 下表是单次交付内部的依赖执行顺序，**不是可中途上线的阶段**——不存在「先交付序 1 再说」。

| 序 | 债项 | 为何这个顺序 |
|----|------|--------------|
| 1 纯净化 | D1 遥测分离 → D2 回填规范化 · D9 PII 修 · D7 INDEX 瘦身 · D8 SQLite 清 · D10 死桩 | D2 依赖 D1 的「纯 prose」目标态 |
| 2 堵入口 | D5 save_memory lane 门 · D6 dream 矛盾门 | 拦新脏须先于清存量，否则边清边脏 |
| 3 接水泵 | D3 merge-on-write+caps · D4 reconsolidation/retirement | 兑现 §4.4/§4.8/§4.9，让存量有界 |

全部 D1-D10 同一轮交付完成；每项**红测先行**（Testcontainers 真 PG）+ **生产实证验收**（对同一批 agent 文件前后复查，绿测试 ≠ 完成）。

---

## 6. 决策（owner 2026-06-08 已拍板）

- **范围**：D1-D10 **全部，单次完整交付，禁 MVP**（交付纪律）。
- **存量脏文件**：一次性回填清洗 → `archive.md`（可逆，保留证据；D2/D4 配套）。
- **D5 拒绝 episodic**：prompt 引导 + 写入点硬门**双保险**。
- **架构**：**0 改动**，不碰 §7 文件合并（spec 明令需完整迁移）。
- **唯一安全门**：D2 回填 / D8 删文件会触碰生产数据卷 → 先 **dry-run + 给 owner 看 diff + 确认**后执行（这是安全门，非 MVP；完整性不豁免安全）。

---

## 附：不碰的范围

- **架构 / spec 本身**：`agent-memory-md-first-spec.md` 是参照系，本文只让生产符合它。
- **Skill 路径**（Harness Agent）：owner 明确「没大问题」，不动。
- **抽取 prompt**（`extract_agent.py`）：已是 L1 质量，不动；只把它的 lane 纪律下沉到 save_memory（D5）。
- **§7 文件合并 / 2 层收敛**：spec 明令需完整迁移，不在本债务清册范围。
