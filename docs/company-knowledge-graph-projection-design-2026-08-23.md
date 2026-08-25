# Company Knowledge 图投影流转设计：证据层抽取 + 审核前置门

> 日期：2026-08-23
> 状态：设计稿，未施工。owner 已确认核心方案（异步抽取 + 审核前置门）
> 代码基线：`0cac825b0683b23f153d260d4708fc2bafcf318a`
> 上游依据：`docs/kimi-review-report-2026-07-24.md`（HN-04 七原子闭环）、`docs/company-knowledge-semantic-layer-decision-memo-2026-07-20.md`（§1.2 两种图的裁决、§9 权限）、`docs/knowledge-substrate-plugin-architecture-2026-07-09.md`（§8 provider 契约）

---

## 0. 一页结论

**问题不是「要不要做抽取/冲突检测/去重/建图」，而是「已经做了两遍，但没接起来」。**

当前图存在于链路两端、中间断裂：Personal KB 有 LLM 抽取的 `knowledge_entities/assertions/links`；Company Ontology 有 curation 策展的 `objects/assertions/links`；而中间的 Company Knowledge 文档层没有图，`conflict_candidates` 与 `ontology_mapping` 两个字段一路铺到 proposal 表却恒为空。

**本设计的单一主张：把抽取下沉到 evidence 层（所有入口的唯一汇聚点），抽取结果作为 proposal 的一部分送审，图作为 publication 的派生投影随发布生效。**

由此：
- 三个入口（`personal_promotion` / `legacy_import` / 直接 source contract ingest）自动获得同等处理深度，不再有「走 Personal 的有图、直接导入的没图」；
- 图不需要独立授权模型——它的权限等于所属 publication 的权限；
- 审核者审的不再是「这份 PDF 收不收」，而是「这份 PDF 会新增哪些事实、与现有知识有哪些冲突」。

---

## 1. 现状：三个实测事实

### 1.1 图在链路中间断裂，并被重抽一遍

```
Agent Memory  T3 知识平面 knowledge/<concept>.md + Relations 双链
     │ candidate（owner consent）
     ▼
Personal KB   documents → segments → knowledge_entities / assertions / links   ← LLM 抽取，带 confidence
     │ promotion（owner consent + pinned revision + attest_scope_change）
     ▼  ⚠️ 图在此丢弃
Company Knowledge（文档层）
              source_contract → source → evidence → documents/segments
              → proposal → review → immutable publication
              conflict_candidates=()   ontology_mapping={}          ← 恒空
              【无实体、无断言、无图】
     ⇅ 完全独立、不共享上游抽取结果
Company Ontology（对象层）
              curation_run（LLM 从 evidence 重新策展）
              → objects / assertions / links / events
              conflict_ledger.unresolved                            ← 唯一真实的冲突检测
              → release（不可变）
```

证据：`app/services/company_knowledge_promotion.py` 内 entity/assertion/link **零命中**——promotion 只搬运文档证据。

### 1.2 冲突检测只在 Ontology 存在

`conflict_candidates` 与 `ontology_mapping` 的赋值点完全同构，四处只有一处真填：

| 位置 | 值 | 场景 |
|---|---|---|
| `tools/handlers/knowledge.py:819-820` | `()` / `{}` | Agent 提案 |
| `services/company_knowledge_service.py:618-619` | `()` / `{}` | Personal→Company promotion、legacy 导入 |
| `api/knowledge_company.py:298-299` | `default_factory` | REST 允许调用方自带，默认空 |
| `services/company_ontology_service.py:1289-1290` | `conflict_ledger.unresolved` / `{curation_run_id: …}` | **Ontology curation，唯一真实** |

字段一路铺到 `models/company_knowledge.py:386-387`（`conflict_candidates_json` / `ontology_mapping_json`）。**这不是遗漏，是设计好了未接线。** 本设计即为其填充逻辑。

### 1.3 去重只有 hash 幂等，无语义去重

`personal_knowledge_proposals.py` 的 `dedupe_key` + `_sha256` 是内容寻址幂等。同一份制度换措辞导入两次 → 两份 publication 并存，无告警。

---

## 2. 核心矛盾：授权粒度不匹配

| | 授权单位 | 是否有权限模型 |
|---|---|---|
| Hive | **publication**（一份发布，带 review / ACL / validity / sensitivity） | 有，且是产品核心 |
| 图 | **triple**（主谓宾一条边） | — |

**一条 LLM 自动抽出的边，谁授权它成为公司事实？** Semantica 无须回答此问（它没有权限模型，抽出即入图）。Hive 必须回答，否则将落入 2026-07-20 memo §1.2 明确警告的失败模式：*「把自动抽取的 GraphRAG edge 当成企业事实」*。

**本设计的回答：不给图独立授权，让图成为 publication 的派生投影。**

- 图的可见性 = 所属 publication 的可见性（继承 `source_acl_snapshot` 与 sensitivity ceiling）
- 图随 publication 的 retire / restore 一同失效或重建
- 图是**可重建的派生物**，不是第二事实源（符合 `knowledge-substrate-plugin-architecture` §8.3「provider 不得成为不能重建、不能替换的唯一索引」）
- 一条边的授权证据 = 该 publication 的 review 记录

---

## 3. 目标流转

### 3.1 抽取挂载点：evidence 层

选择 evidence 层的三个理由：

1. **唯一汇聚点**：HN-04B 已保证「active SourceContract → CanonicalEvidenceEnvelope 是唯一摄取路径」。在此做，三个入口自动覆盖，无需逐入口改造。
2. **权限有据**：`company_knowledge_evidence` 已带 `source_acl_snapshot_hash` + `source_acl_snapshot_json`，抽取结果可直接继承来源 ACL。
3. **时间语义有据**：该表已具备完整双时间轴 `occurred_at` / `effective_from` / `effective_until` / `observed_at`（`models/company_knowledge.py:166-222`），抽出的事实可继承生效区间，无需另造时间模型。

### 3.2 端到端流转

```
① 摄取（同步，快）
   任一入口 → SourceContract 校验 → Evidence Envelope 落库
   → Company KnowledgeDocument/Segment
   → proposal 落 draft/submitted
   → 同事务 enqueue 分析作业（outbox 模式，与 authority commit 同事务）

② 分析（异步，LLM）
   evolution daemon claim 分析作业（lease 防并发）
   → 读 evidence 全文（L1：完整视野，不截断）
   → LLM 抽取 entity / assertion / link 候选 + 冲突台账 + 去重候选
   → 结果写 extraction 表 + 回填 proposal.conflict_candidates_json / ontology_mapping_json
   → 作业 completed；失败则 typed error + 有界重试；无模型配置 → held（不降级、不机械兜底）

③ 审核（人工，有前置门）
   proposal 只有在分析作业 completed 后才允许 begin_review
   → 审核者看到：文档正文 + 将新增/改动的事实 + 与现有知识的冲突点 + 疑似重复
   → approve / reject / request_changes

④ 发布（同步，事务）
   publish 在同一事务内：固定 review-set hash、发布 publication、
   将已批准的抽取候选物化为图投影（继承 publication 的 ACL/validity/sensitivity）
   → event + outbox

⑤ 消费
   文档检索走既有 gateway；图查询走 OntologyEnginePlugin
   图投影可从 publication + extraction 完整重建
```

### 3.3 审核前置门：守卫，不是新状态

现有 proposal 状态机（`services/company_knowledge_contracts.py:299-308`）保持**不变**：

```
draft --submit--> submitted --begin_review--> in_review --approve--> approved
                                                        --reject--> rejected
changes_requested --submit--> submitted
approved --begin_publish--> publishing --publish_succeeded--> published
```

**门加在 `submitted --begin_review--> in_review` 这一个转移上**，不新增状态。

理由：新增 `pending_analysis` 状态会迫使所有既有消费者（API / 前端 / review queue / 报表）认识新状态；而守卫只影响一个转移点，其余消费者零改动。UI 上的「分析中」由分析作业状态派生（read model），不进入 authority 状态机。

按 CLAUDE.md 硬门要求，本门四要素齐备：

| 要素 | 内容 |
|---|---|
| 不变量 | 该 proposal 的分析作业 status = `completed` |
| 权威事实源 | 分析作业表的 `status` 列（机械可验证，非自然语言判断） |
| 被阻断的效果 | `begin_review` 状态转移（不阻断 withdraw、不阻断查看） |
| 可达修复路径 | 等待作业完成；`failed` 可显式 retry；`held`（无模型配置）可修配置后 retry；**授权 override**（见 §3.4） |

门是**机械的**：它只读作业状态，不解释语义内容。符合 Model Agency Boundary 的硬约束允许清单第 5 项（证据与恢复）。

### 3.4 override：门必须有旁路，但旁路必须留痕

现实中会出现分析长期不可用（provider 故障、无模型配置）而业务必须发布的情况。若门无旁路，它就成了阻塞主路径的控制——违反「accepted primary path must remain usable」。

**override 契约：**
- 需要显式权限（不复用 `review` 权限，单独的 `override_analysis_gate`）
- 必须填写理由（非空）
- 写入 Company hash-chain event，`override=true` 与理由一并入 review receipt
- publication 上标记 `graph_projection_status = skipped_by_override`，UI 可见
- **不允许**因 override 而伪造空的冲突台账——台账保持「未就绪」的真实状态，不写成「无冲突」

---

## 4. 新增数据结构

### 4.1 分析作业表（照抄 import_job 的成熟形状）

`company_knowledge_analysis_jobs`，字段形状复用 `company_knowledge_import_jobs`（`models/company_knowledge.py:223-300`）已验证的模式：

```
status            queued | running | completed | held | failed | cancelled
available_at      延迟调度
claim_token / claim_expires_at    lease 防并发
attempt_count / max_attempts      有界重试
last_error_code / last_error      typed 错误
idempotency_key / request_hash    幂等
evidence_id / document_id / proposal_id    结果绑定
tenant_id + RLS ENABLE & FORCE
trace_id / created_by_type / created_by_id / accountable_user_id
```

**复用既有 evolution daemon**：HN-04B 已实现「daemon 扫描 queued/failed/stale-running jobs 并重新进入同一 canonical processor」。分析作业接入同一扫描器，不新建 daemon。

### 4.2 抽取候选表

`company_knowledge_extractions`，存 LLM 抽取产物，按 `evidence_id` + `content_hash` 幂等：

```
evidence_id / proposal_id / tenant_id
entities_json      候选实体（canonical_name, type, aliases, confidence）
assertions_json    候选断言（subject, predicate, object, confidence, segment_ref）
links_json         候选关系
conflict_ledger_json   与现有 publication/release 的冲突台账（含被冲突方的 publication_id）
dedupe_candidates_json 疑似重复（含相似依据与被疑重的 publication_id）
coverage_json      覆盖账本：哪些 segment 已分析、哪些未覆盖及原因
model_receipt_json 模型/prompt receipt（沿用 ontology curation 的可信 receipt 做法）
content_hash       抽取输入的内容哈希，用于幂等与失效判定
```

**候选就是候选**：本表内容不是公司事实，未经 publish 物化前不进入任何查询路径。

### 4.3 图投影

不新建实体表。已批准的抽取候选在 publish 时物化进**既有** `company_ontology_objects / assertions / links`，并通过既有 `company_ontology_evidence_bindings` 绑定回 evidence。

这样做的收益：Ontology 侧的 query / release / RLS / 溯源全部复用，`OntologyEnginePlugin` 也自然能查到这些事实——**文档层与对象层由此接通，不再是两条平行路**。

---

## 5. 智能与机械的分工（AI-Native L1 / Model Agency Boundary）

| 职责 | 归属 | 说明 |
|---|---|---|
| 抽实体/断言/关系 | **LLM** | 完整 evidence 视野，不截断；输出预算按内容规模给足 |
| 判断「是否与现有知识冲突」 | **LLM** | 语义判断。检索出的候选对照集由平台提供，是否构成冲突由模型判定并给出理由 |
| 判断「是否重复」 | **LLM** | 同上。平台只提供相似候选，不用阈值代替判断 |
| 提供对照候选集 | 平台 | 按实体/主谓宾/命名空间检索现有 publication 与 release |
| 幂等、去重键、hash | 平台 | 机械事实 |
| ACL / sensitivity / validity 继承 | 平台 | 机械事实，来自 evidence 快照 |
| 门的开合 | 平台 | 只读作业状态，不解释语义 |
| 批准与否 | **人** | 授权决定 |
| 物化落盘 | 平台 | 事务、事件、outbox |

**禁止**：用相似度阈值直接判定重复并自动丢弃；用关键词/正则判定冲突；LLM 不可用时用机械规则产出「无冲突」的台账（必须 held，见 §3.3）。

---

## 6. 七原子映射

| 原子 | 落点 |
|---|---|
| 输入 | 三入口统一经 SourceContract → Evidence Envelope；分析作业请求含 `idempotency_key` + `request_hash` |
| 权威 | 图投影无独立权威，继承 publication；override 需专门权限；候选表非事实源 |
| 执行 | 唯一入口：evidence 落库同事务 enqueue → daemon canonical processor → publish 物化。无旁路 |
| 证据 | 抽取产物 + coverage 账本 + model receipt + 冲突台账全部持久化；Company hash-chain event 记录 enqueue/completed/override/物化 |
| 恢复 | 作业 lease + 有界重试 + typed error；`held` 可修配置后重试；publish 事务失败整体回滚；图投影可从 publication + extraction 完整重建 |
| 消费 | 审核界面消费冲突台账与候选；`OntologyEnginePlugin` 消费物化后的图；文档检索路径不变 |
| 验收 | 见 §8 |

---

## 7. 完整施工范围（一次改完，非 MVP）

按 Delivery Discipline，本轮范围一次交付，不分期：

1. **schema**：`company_knowledge_analysis_jobs` + `company_knowledge_extractions` 两表，单一 alembic revision，migration 与 fresh-bootstrap 两条路径均 `ENABLE + FORCE RLS`；downgrade 在存在运行时数据时明确阻断。
2. **enqueue**：三个入口在 evidence 落库的同一事务内入队（outbox 模式，不自行 commit）。
3. **processor**：canonical 分析处理器 + 接入既有 evolution daemon 扫描。
4. **回填**：分析完成后写 `proposal.conflict_candidates_json` / `ontology_mapping_json`（**填充既有字段，不新增**）。
5. **门**：`begin_review` 守卫 + `override_analysis_gate` 权限 + event 留痕。
6. **物化**：publish 事务内将已批准候选写入 `company_ontology_objects/assertions/links` + `evidence_bindings`。
7. **retire/restore 联动**：publication 失效时图投影同步失效；restore 创建新版本并重建投影。
8. **backfill**：对**已存在的** publication 补跑分析（当前生产 28 张表 0 行，backfill 面为空，但代码路径必须具备且可对存量执行）。
9. **前端**：审核界面展示冲突台账/去重候选/覆盖账本；proposal 列表显示「分析中」派生状态；override 需填理由；en + zh 双语同步。
10. **观测**：作业积压与失败率指标、`held` 告警、override 计数。
11. **清理**：不留 feature flag 默认关闭的半成品；不保留旧的空值传参路径。

---

## 8. 验收标准

- **红测先行**：每项契约先红后绿，贴数字。
- 三入口（personal_promotion / legacy_import / 直接 ingest）各自产生非空冲突台账的集成测试。
- 门测试：分析未完成 → `begin_review` 被拒且返回 typed 状态；作业 `failed` → retry 后可评审；`held` → 修配置后可评审。
- override 测试：无权限被拒；有权限但理由为空被拒；成功 override 写入 event 且 publication 标记 `skipped_by_override`；台账不被伪造为「无冲突」。
- LLM 不可用 → 作业 `held`，proposal 停在 `submitted`，**无机械兜底台账**。
- publish 事务失败 → 零图投影副作用、零 publication。
- retire → 图投影失效；restore → 新版本 + 投影重建。
- 幂等：同 `request_hash` 重放不产生第二份抽取；不同 payload 同 key → fail closed。
- RLS：新增两表在 migration 与 fresh-bootstrap 双路径 `27/27`（新增后为 30/30）FORCE RLS；NOBYPASSRLS 角色跨租户为 0。
- 真 PG 端到端：ingest → 分析 → 冲突台账 → 审核 → publish → 图查询 → retire → restore → event chain。
- 前端：i18n en/zh 键数对齐、审核界面测试、构建 bundle budget 通过。

---

## 9. 明确不做

1. **不给图独立授权模型** —— 图是 publication 的派生投影（§2）。
2. **不新增 proposal 状态** —— 门是转移守卫（§3.3）。
3. **不放开 Agent Memory 直连 Company** —— 保持必经 Personal 的 owner consent 断点。Agent Memory 是 agent 主观自述，证据强度与 owner 亲自放入的文档不同级；那道 consent 是「agent 认为」变成「人确认」的唯一断点。若需降低摩擦，做「自动候选 + owner 一键批准」，不取消断点。
4. **不引入向量库做去重** —— 本轮去重候选由既有 FTS + 实体匹配提供对照集，语义判断归 LLM。是否引入 dense 由生产真实召回数据决定（沿用 §HN-04C「FTS/ILIKE baseline 是有意基线」的裁决）。**memory 层维持 `memory-system-spec.md` §0 硬约束「不上 vector」不变。**
5. **不在本轮接入 Semantica** —— 图投影接通后，`OntologyEnginePlugin` 的递归推理增强仍按 `memory-ontology-external-baseline-evaluation-2026-08-17.md` v2 §2.3 处理：可行、零摩擦、不补断点、不排期。
6. **不改 Personal KB 既有抽取** —— Personal 侧 `knowledge_entities/assertions/links` 保持现状。promotion 时其抽取结果可作为 Company 侧分析的**先验输入**（降低成本），但不直接充当 Company 事实，仍须重新过审。
7. **不做全库回溯重抽** —— backfill 按 publication 显式触发，不后台全量扫描。

---

## 10. 待定与风险

| # | 项 | 现状 |
|---|---|---|
| R1 | **分析成本**：每份 evidence 一次 LLM 全文抽取。大 PDF/音视频转写后体量可观 | 需要 owner 定成本上限策略：按 tenant 配额、按 sensitivity 分级、或超阈值转人工触发。**未定** |
| R2 | **冲突对照集的检索质量**：中文 `FTS('simple')` 不分词，会漏掉应当被发现的冲突 | 这是第一个**真实业务后果**取决于中文检索质量的场景。B3 验收若显示漏检严重，需重新评估 dense 的优先级 |
| R3 | **override 滥用** | 权限 + 理由 + event + publication 标记四重留痕；建议加运营看板监控 override 率 |
| R4 | **音视频入口** | 本设计假定 evidence 已是文本（转写后）。ASR/OCR 环节属 import provider 职责，不在本设计范围；但 coverage 账本须能表达「该 evidence 含未转写片段」 |
| R5 | **Ontology package 未安装时的物化** | 抽取候选可能引用尚未定义的 object type。需定：拒绝物化并要求先发布 ontology 变更，或允许 freeform 落地后再归类。**建议前者**（保持类型受控），待 owner 确认 |

---

## 11. 修订记录

| 日期 | 变更 |
|---|---|
| 2026-08-23 | 首版。确立「抽取下沉 evidence 层 + 图作为 publication 派生投影 + begin_review 守卫门 + override 留痕」四项核心设计；owner 已确认异步 + 审核前置门方案 |
